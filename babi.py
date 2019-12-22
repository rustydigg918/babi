import argparse
import collections
import contextlib
import curses
import enum
import functools
import hashlib
import io
import os
import re
import signal
import sys
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import FrozenSet
from typing import Generator
from typing import IO
from typing import Iterator
from typing import List
from typing import Match
from typing import NamedTuple
from typing import Optional
from typing import Pattern
from typing import Tuple
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import Union

if TYPE_CHECKING:
    from typing import Protocol  # python3.8+
else:
    Protocol = object

VERSION_STR = 'babi v0'
TCallable = TypeVar('TCallable', bound=Callable[..., Any])
EditResult = enum.Enum('EditResult', 'EXIT NEXT PREV')
PromptResult = enum.Enum('PromptResult', 'CANCELLED')


def _line_x(x: int, width: int) -> int:
    margin = min(width - 3, 6)
    if x + 1 < width:
        return 0
    elif width == 1:
        return x
    else:
        return (
            width - margin - 2 +
            (x + 1 - width) //
            (width - margin - 2) *
            (width - margin - 2)
        )


def _scrolled_line(s: str, x: int, width: int, *, current: bool) -> str:
    line_x = _line_x(x, width)
    if current and line_x:
        s = f'«{s[line_x + 1:]}'
        if line_x and len(s) > width:
            return f'{s[:width - 1]}»'
        else:
            return s.ljust(width)
    elif len(s) > width:
        return f'{s[:width - 1]}»'
    else:
        return s.ljust(width)


class MutableSequenceNoSlice(Protocol):
    def __len__(self) -> int: ...
    def __getitem__(self, idx: int) -> str: ...
    def __setitem__(self, idx: int, val: str) -> None: ...
    def __delitem__(self, idx: int) -> None: ...
    def insert(self, idx: int, val: str) -> None: ...

    def __iter__(self) -> Iterator[str]:
        for i in range(len(self)):
            yield self[i]

    def append(self, val: str) -> None:
        self.insert(len(self), val)

    def pop(self, idx: int = -1) -> str:
        victim = self[idx]
        del self[idx]
        return victim


def _del(lst: MutableSequenceNoSlice, *, idx: int) -> None:
    del lst[idx]


def _set(lst: MutableSequenceNoSlice, *, idx: int, val: str) -> None:
    lst[idx] = val


def _ins(lst: MutableSequenceNoSlice, *, idx: int, val: str) -> None:
    lst.insert(idx, val)


class ListSpy(MutableSequenceNoSlice):
    def __init__(self, lst: MutableSequenceNoSlice) -> None:
        self._lst = lst
        self._undo: List[Callable[[MutableSequenceNoSlice], None]] = []

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self._lst})'

    def __len__(self) -> int:
        return len(self._lst)

    def __getitem__(self, idx: int) -> str:
        return self._lst[idx]

    def __setitem__(self, idx: int, val: str) -> None:
        self._undo.append(functools.partial(_set, idx=idx, val=self._lst[idx]))
        self._lst[idx] = val

    def __delitem__(self, idx: int) -> None:
        if idx < 0:
            idx %= len(self)
        self._undo.append(functools.partial(_ins, idx=idx, val=self._lst[idx]))
        del self._lst[idx]

    def insert(self, idx: int, val: str) -> None:
        if idx < 0:
            idx %= len(self)
        self._undo.append(functools.partial(_del, idx=idx))
        self._lst.insert(idx, val)

    def undo(self, lst: MutableSequenceNoSlice) -> None:
        for fn in reversed(self._undo):
            fn(lst)

    @property
    def has_modifications(self) -> bool:
        return bool(self._undo)


class Margin(NamedTuple):
    header: bool
    footer: bool

    @property
    def body_lines(self) -> int:
        return curses.LINES - self.header - self.footer

    @property
    def page_size(self) -> int:
        if self.body_lines <= 2:
            return 1
        else:
            return self.body_lines - 2

    @classmethod
    def from_screen(cls, screen: 'curses._CursesWindow') -> 'Margin':
        if curses.LINES == 1:
            return cls(header=False, footer=False)
        elif curses.LINES == 2:
            return cls(header=False, footer=True)
        else:
            return cls(header=True, footer=True)


def _get_color_pair_mapping() -> Dict[Tuple[int, int], int]:
    ret = {}
    i = 0
    for bg in range(-1, 16):
        for fg in range(bg, 16):
            ret[(fg, bg)] = i
            i += 1
    return ret


COLORS = _get_color_pair_mapping()
del _get_color_pair_mapping


def _has_colors() -> bool:
    return curses.has_colors and curses.COLORS >= 16


def _color(fg: int, bg: int) -> int:
    if _has_colors():
        if bg > fg:
            return curses.A_REVERSE | curses.color_pair(COLORS[(bg, fg)])
        else:
            return curses.color_pair(COLORS[(fg, bg)])
    else:
        if bg > fg:
            return curses.A_REVERSE | curses.color_pair(0)
        else:
            return curses.color_pair(0)


def _init_colors(stdscr: 'curses._CursesWindow') -> None:
    curses.use_default_colors()
    if not _has_colors():
        return
    for (fg, bg), pair in COLORS.items():
        if pair == 0:  # cannot reset pair 0
            continue
        curses.init_pair(pair, fg, bg)


class Status:
    def __init__(self) -> None:
        self._status = ''
        self._action_counter = -1
        self._history: Dict[str, List[str]] = collections.defaultdict(list)
        self._history_orig_len: Dict[str, int] = collections.defaultdict(int)
        self._history_prev: Dict[str, str] = {}

    @contextlib.contextmanager
    def save_history(self) -> Generator[None, None, None]:
        history_dir = os.path.join(
            os.environ.get('XDG_DATA_HOME') or
            os.path.expanduser('~/.local/share'),
            'babi/history',
        )
        os.makedirs(history_dir, exist_ok=True)
        for filename in os.listdir(history_dir):
            with open(os.path.join(history_dir, filename)) as f:
                self._history[filename] = f.read().splitlines()
                self._history_orig_len[filename] = len(self._history[filename])
        try:
            yield
        finally:
            for k, v in self._history.items():
                new_history = v[self._history_orig_len[k]:]
                if new_history:
                    with open(os.path.join(history_dir, k), 'a+') as f:
                        f.write('\n'.join(new_history) + '\n')

    def update(self, status: str) -> None:
        self._status = status
        self._action_counter = 25

    def clear(self) -> None:
        self._status = ''

    def draw(self, stdscr: 'curses._CursesWindow', margin: Margin) -> None:
        if margin.footer or self._status:
            stdscr.insstr(curses.LINES - 1, 0, ' ' * curses.COLS)
            if self._status:
                status = f' {self._status} '
                x = (curses.COLS - len(status)) // 2
                if x < 0:
                    x = 0
                    status = status.strip()
                stdscr.insstr(curses.LINES - 1, x, status, curses.A_REVERSE)

    def tick(self, margin: Margin) -> None:
        # when the window is only 1-tall, hide the status quicker
        if margin.footer:
            self._action_counter -= 1
        else:
            self._action_counter -= 24
        if self._action_counter < 0:
            self.clear()

    def _cancel(self) -> Union[str, PromptResult]:
        self.update('cancelled')
        return PromptResult.CANCELLED

    def prompt(
            self,
            screen: 'Screen',
            prompt: str,
            *,
            allow_empty: bool = False,
            history: Optional[str] = None,
            default_prev: bool = False,
            default: Optional[str] = None,
    ) -> Union[str, PromptResult]:
        self.clear()
        default = default or ''
        if history is not None:
            lst = [*self._history[history], default]
            lst_pos = len(lst) - 1
            if default_prev and history in self._history_prev:
                prompt = f'{prompt} [{self._history_prev[history]}]'
        else:
            lst = [default]
            lst_pos = 0
        pos = 0

        def buf() -> str:
            return lst[lst_pos]

        def set_buf(s: str) -> None:
            lst[lst_pos] = s

        def _save_history_and_get_retv() -> Union[str, PromptResult]:
            if history is not None:
                prev = self._history_prev.get(history)
                entry = buf()
                if entry:  # only put non-empty things in history
                    history_lst = self._history[history]
                    if not history_lst or history_lst[-1] != entry:
                        history_lst.append(entry)
                    self._history_prev[history] = entry

                if (
                        default_prev and
                        prev is not None and
                        lst_pos == len(lst) - 1 and
                        not lst[lst_pos]
                ):
                    return prev

            if not allow_empty and not buf():
                return self._cancel()
            else:
                return buf()

        def _render_prompt(*, base: str = prompt) -> None:
            if not base or curses.COLS < 7:
                prompt_s = ''
            elif len(base) > curses.COLS - 6:
                prompt_s = f'{base[:curses.COLS - 7]}…: '
            else:
                prompt_s = f'{base}: '
            width = curses.COLS - len(prompt_s)
            line = _scrolled_line(lst[lst_pos], pos, width, current=True)
            cmd = f'{prompt_s}{line}'
            screen.stdscr.insstr(curses.LINES - 1, 0, cmd, curses.A_REVERSE)
            line_x = _line_x(pos, width)
            screen.stdscr.move(curses.LINES - 1, len(prompt_s) + pos - line_x)

        while True:
            _render_prompt()
            key = _get_char(screen.stdscr)

            if key.key == curses.KEY_RESIZE:
                screen.resize()
            elif key.key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key.key == curses.KEY_RIGHT:
                pos = min(len(lst[lst_pos]), pos + 1)
            elif key.key == curses.KEY_UP:
                lst_pos = max(0, lst_pos - 1)
                pos = len(lst[lst_pos])
            elif key.key == curses.KEY_DOWN:
                lst_pos = min(len(lst) - 1, lst_pos + 1)
                pos = len(lst[lst_pos])
            elif key.key == curses.KEY_HOME or key.keyname == b'^A':
                pos = 0
            elif key.key == curses.KEY_END or key.keyname == b'^E':
                pos = len(lst[lst_pos])
            elif key.key == curses.KEY_BACKSPACE:
                if pos > 0:
                    set_buf(buf()[:pos - 1] + buf()[pos:])
                    pos -= 1
            elif key.key == curses.KEY_DC:
                if pos < len(lst[lst_pos]):
                    set_buf(buf()[:pos] + buf()[pos + 1:])
            elif isinstance(key.wch, str) and key.wch.isprintable():
                set_buf(buf()[:pos] + key.wch + buf()[pos:])
                pos += 1
            elif key.keyname == b'^R':
                reverse_s = ''
                reverse_idx = lst_pos
                while True:
                    reverse_failed = False
                    for search_idx in range(reverse_idx, -1, -1):
                        if reverse_s in lst[search_idx]:
                            reverse_idx = lst_pos = search_idx
                            pos = len(buf())
                            break
                    else:
                        reverse_failed = True

                    if reverse_failed:
                        base = f'{prompt}(failed reverse-search)`{reverse_s}`'
                    else:
                        base = f'{prompt}(reverse-search)`{reverse_s}`'

                    _render_prompt(base=base)
                    key = _get_char(screen.stdscr)

                    if key.key == curses.KEY_RESIZE:
                        screen.resize()
                    elif key.key == curses.KEY_BACKSPACE:
                        reverse_s = reverse_s[:-1]
                    elif isinstance(key.wch, str) and key.wch.isprintable():
                        reverse_s += key.wch
                    elif key.keyname == b'^R':
                        reverse_idx = max(0, reverse_idx - 1)
                    elif key.keyname == b'^C':
                        return self._cancel()
                    elif key.key == ord('\r'):
                        return _save_history_and_get_retv()
                    else:
                        # python3.8+ optimizes this out
                        # https://github.com/nedbat/coveragepy/issues/772
                        break  # pragma: no cover

            elif key.keyname == b'^C':
                return self._cancel()
            elif key.key == ord('\r'):
                return _save_history_and_get_retv()

    def quick_prompt(
            self,
            screen: 'Screen',
            prompt: str,
            options: FrozenSet[str],
            *,
            resize: Optional[Callable[[], None]] = None,
    ) -> Union[str, PromptResult]:
        while True:
            s = prompt.ljust(curses.COLS)
            if len(s) > curses.COLS:
                s = f'{s[:curses.COLS - 1]}…'
            screen.stdscr.insstr(curses.LINES - 1, 0, s, curses.A_REVERSE)
            x = min(curses.COLS - 1, len(prompt) + 1)
            screen.stdscr.move(curses.LINES - 1, x)

            key = _get_char(screen.stdscr)
            if key.key == curses.KEY_RESIZE:
                screen.resize()
                if resize is not None:
                    resize()
            elif key.keyname == b'^C':
                return self._cancel()
            elif key.wch in options:
                assert isinstance(key.wch, str)  # mypy doesn't know
                return key.wch


def _restore_lines_eof_invariant(lines: MutableSequenceNoSlice) -> None:
    """The file lines will always contain a blank empty string at the end to
    simplify rendering.  This should be called whenever the end of the file
    might change.
    """
    if not lines or lines[-1] != '':
        lines.append('')


def _get_lines(sio: IO[str]) -> Tuple[List[str], str, bool, str]:
    sha256 = hashlib.sha256()
    lines = []
    newlines = collections.Counter({'\n': 0})  # default to `\n`
    for line in sio:
        sha256.update(line.encode())
        for ending in ('\r\n', '\n'):
            if line.endswith(ending):
                lines.append(line[:-1 * len(ending)])
                newlines[ending] += 1
                break
        else:
            lines.append(line)
    _restore_lines_eof_invariant(lines)
    (nl, _), = newlines.most_common(1)
    mixed = len({k for k, v in newlines.items() if v}) > 1
    return lines, nl, mixed, sha256.hexdigest()


class Action:
    def __init__(
            self, *, name: str, spy: ListSpy,
            start_x: int, start_y: int, start_modified: bool,
            end_x: int, end_y: int, end_modified: bool,
            final: bool,
    ):
        self.name = name
        self.spy = spy
        self.start_x = start_x
        self.start_y = start_y
        self.start_modified = start_modified
        self.end_x = end_x
        self.end_y = end_y
        self.end_modified = end_modified
        self.final = final

    def apply(self, file: 'File') -> 'Action':
        spy = ListSpy(file.lines)
        action = Action(
            name=self.name, spy=spy,
            start_x=self.end_x, start_y=self.end_y,
            start_modified=self.end_modified,
            end_x=self.start_x, end_y=self.start_y,
            end_modified=self.start_modified,
            final=True,
        )

        self.spy.undo(spy)
        file.x = self.start_x
        file.cursor_y = self.start_y
        file.modified = self.start_modified

        return action


def action(func: TCallable) -> TCallable:
    @functools.wraps(func)
    def action_inner(self: 'File', *args: Any, **kwargs: Any) -> Any:
        assert not isinstance(self.lines, ListSpy), 'nested edit/movement'
        self.mark_previous_action_as_final()
        return func(self, *args, **kwargs)
    return cast(TCallable, action_inner)


def edit_action(
        name: str,
        *,
        final: bool,
) -> Callable[[TCallable], TCallable]:
    def edit_action_decorator(func: TCallable) -> TCallable:
        @functools.wraps(func)
        def edit_action_inner(self: 'File', *args: Any, **kwargs: Any) -> Any:
            with self.edit_action_context(name, final=final):
                return func(self, *args, **kwargs)
        return cast(TCallable, edit_action_inner)
    return edit_action_decorator


class Found(NamedTuple):
    y: int
    match: Match[str]


class _SearchIter:
    def __init__(
            self,
            file: 'File',
            reg: Pattern[str],
            *,
            offset: int,
    ) -> None:
        self.file = file
        self.reg = reg
        self.offset = offset
        self.wrapped = False
        self._start_x = file.x + offset
        self._start_y = file.cursor_y

    def __iter__(self) -> '_SearchIter':
        return self

    def _stop_if_past_original(self, y: int, match: Match[str]) -> Found:
        if (
                self.wrapped and (
                    y > self._start_y or
                    y == self._start_y and match.start() >= self._start_x
                )
        ):
            raise StopIteration()
        return Found(y, match)

    def __next__(self) -> Tuple[int, Match[str]]:
        x = self.file.x + self.offset
        y = self.file.cursor_y

        match = self.reg.search(self.file.lines[y], x)
        if match:
            return self._stop_if_past_original(y, match)

        if self.wrapped:
            for line_y in range(y + 1, self._start_y + 1):
                match = self.reg.search(self.file.lines[line_y])
                if match:
                    return self._stop_if_past_original(line_y, match)
        else:
            for line_y in range(y + 1, len(self.file.lines)):
                match = self.reg.search(self.file.lines[line_y])
                if match:
                    return self._stop_if_past_original(line_y, match)

            self.wrapped = True

            for line_y in range(0, self._start_y + 1):
                match = self.reg.search(self.file.lines[line_y])
                if match:
                    return self._stop_if_past_original(line_y, match)

        raise StopIteration()


class File:
    def __init__(self, filename: Optional[str]) -> None:
        self.filename = filename
        self.modified = False
        self.lines: MutableSequenceNoSlice = []
        self.nl = '\n'
        self.file_y = self.cursor_y = self.x = self.x_hint = 0
        self.sha256: Optional[str] = None
        self.undo_stack: List[Action] = []
        self.redo_stack: List[Action] = []

    def ensure_loaded(self, status: Status) -> None:
        if self.lines:
            return

        if self.filename is not None and os.path.isfile(self.filename):
            with open(self.filename, newline='') as f:
                self.lines, self.nl, mixed, self.sha256 = _get_lines(f)
        else:
            if self.filename is not None:
                if os.path.lexists(self.filename):
                    status.update(f'{self.filename!r} is not a file')
                    self.filename = None
                else:
                    status.update('(new file)')
            sio = io.StringIO('')
            self.lines, self.nl, mixed, self.sha256 = _get_lines(sio)

        if mixed:
            status.update(f'mixed newlines will be converted to {self.nl!r}')
            self.modified = True

    def __repr__(self) -> str:
        attrs = ',\n    '.join(f'{k}={v!r}' for k, v in self.__dict__.items())
        return f'{type(self).__name__}(\n    {attrs},\n)'

    # movement

    def scroll_screen_if_needed(self, margin: Margin) -> None:
        # if the `cursor_y` is not on screen, make it so
        if self.file_y <= self.cursor_y < self.file_y + margin.body_lines:
            return

        self.file_y = max(self.cursor_y - margin.body_lines // 2, 0)

    def _scroll_amount(self) -> int:
        return int(curses.LINES / 2 + .5)

    def _set_x_after_vertical_movement(self) -> None:
        self.x = min(len(self.lines[self.cursor_y]), self.x_hint)

    def _increment_y(self, margin: Margin) -> None:
        self.cursor_y += 1
        if self.cursor_y >= self.file_y + margin.body_lines:
            self.file_y += self._scroll_amount()

    def _decrement_y(self, margin: Margin) -> None:
        self.cursor_y -= 1
        if self.cursor_y < self.file_y:
            self.file_y -= self._scroll_amount()
            self.file_y = max(self.file_y, 0)

    @action
    def down(self, margin: Margin) -> None:
        if self.cursor_y < len(self.lines) - 1:
            self._increment_y(margin)
            self._set_x_after_vertical_movement()

    @action
    def up(self, margin: Margin) -> None:
        if self.cursor_y > 0:
            self._decrement_y(margin)
            self._set_x_after_vertical_movement()

    @action
    def right(self, margin: Margin) -> None:
        if self.x >= len(self.lines[self.cursor_y]):
            if self.cursor_y < len(self.lines) - 1:
                self.x = 0
                self._increment_y(margin)
        else:
            self.x += 1
        self.x_hint = self.x

    @action
    def left(self, margin: Margin) -> None:
        if self.x == 0:
            if self.cursor_y > 0:
                self._decrement_y(margin)
                self.x = len(self.lines[self.cursor_y])
        else:
            self.x -= 1
        self.x_hint = self.x

    @action
    def home(self, margin: Margin) -> None:
        self.x = self.x_hint = 0

    @action
    def end(self, margin: Margin) -> None:
        self.x = self.x_hint = len(self.lines[self.cursor_y])

    @action
    def ctrl_home(self, margin: Margin) -> None:
        self.x = self.x_hint = 0
        self.cursor_y = self.file_y = 0

    @action
    def ctrl_end(self, margin: Margin) -> None:
        self.x = self.x_hint = 0
        self.cursor_y = len(self.lines) - 1
        self.scroll_screen_if_needed(margin)

    @action
    def ctrl_up(self, margin: Margin) -> None:
        self.file_y = max(0, self.file_y - 1)
        self.cursor_y = min(self.cursor_y, self.file_y + margin.body_lines - 1)
        self._set_x_after_vertical_movement()

    @action
    def ctrl_down(self, margin: Margin) -> None:
        self.file_y = min(len(self.lines) - 1, self.file_y + 1)
        self.cursor_y = max(self.cursor_y, self.file_y)
        self._set_x_after_vertical_movement()

    @action
    def ctrl_right(self, margin: Margin) -> None:
        line = self.lines[self.cursor_y]
        # if we're at the second to last character, jump to end of line
        if self.x == len(line) - 1:
            self.x = self.x_hint = self.x + 1
        # if we're at the end of the line, jump forward to the next non-ws
        elif self.x == len(line):
            while (
                    self.cursor_y < len(self.lines) - 1 and (
                        self.x == len(self.lines[self.cursor_y]) or
                        self.lines[self.cursor_y][self.x].isspace()
                    )
            ):
                if self.x == len(self.lines[self.cursor_y]):
                    self._increment_y(margin)
                    self.x = self.x_hint = 0
                else:
                    self.x = self.x_hint = self.x + 1
        # if we're inside the line, jump to next position that's not our type
        else:
            self.x = self.x_hint = self.x + 1
            isalnum = line[self.x].isalnum()
            while self.x < len(line) and isalnum == line[self.x].isalnum():
                self.x = self.x_hint = self.x + 1

    @action
    def ctrl_left(self, margin: Margin) -> None:
        line = self.lines[self.cursor_y]
        # if we're at position 1 and it's not a space, go to the beginning
        if self.x == 1 and not line[:self.x].isspace():
            self.x = self.x_hint = 0
        # if we're at the beginning or it's all space up to here jump to the
        # end of the previous non-space line
        elif self.x == 0 or line[:self.x].isspace():
            self.x = self.x_hint = 0
            while (
                    self.cursor_y > 0 and (
                        self.x == 0 or
                        not self.lines[self.cursor_y]
                    )
            ):
                self._decrement_y(margin)
                self.x = self.x_hint = len(self.lines[self.cursor_y])
        else:
            self.x = self.x_hint = self.x - 1
            isalnum = line[self.x - 1].isalnum()
            while self.x > 0 and isalnum == line[self.x - 1].isalnum():
                self.x = self.x_hint = self.x - 1

    @action
    def go_to_line(self, lineno: int, margin: Margin) -> None:
        self.x = self.x_hint = 0
        if lineno == 0:
            self.cursor_y = 0
        elif lineno > len(self.lines):
            self.cursor_y = len(self.lines) - 1
        elif lineno < 0:
            self.cursor_y = max(0, lineno + len(self.lines))
        else:
            self.cursor_y = lineno - 1
        self.scroll_screen_if_needed(margin)

    @action
    def search(
            self,
            reg: Pattern[str],
            status: Status,
            margin: Margin,
    ) -> None:
        search = _SearchIter(self, reg, offset=1)
        try:
            line_y, match = next(iter(search))
        except StopIteration:
            status.update('no matches')
        else:
            if line_y == self.cursor_y and match.start() == self.x:
                status.update('this is the only occurrence')
            else:
                if search.wrapped:
                    status.update('search wrapped')
                self.cursor_y = line_y
                self.x = self.x_hint = match.start()
                self.scroll_screen_if_needed(margin)

    def replace(
            self,
            screen: 'Screen',
            reg: Pattern[str],
            replace: str,
    ) -> None:
        self.mark_previous_action_as_final()

        def highlight() -> None:
            y = screen.file.rendered_y(screen.margin)
            x = screen.file.rendered_x()
            maxlen = curses.COLS - x
            s = match[0]
            if len(s) >= maxlen:
                s = _scrolled_line(match[0], 0, maxlen, current=True)
            screen.stdscr.addstr(y, x, s, curses.A_REVERSE)

        count = 0
        res: Union[str, PromptResult] = ''
        search = _SearchIter(self, reg, offset=0)
        for line_y, match in search:
            self.cursor_y = line_y
            self.x = self.x_hint = match.start()
            self.scroll_screen_if_needed(screen.margin)
            if res != 'a':  # make `a` replace the rest of them
                screen.draw()
                highlight()
                res = screen.status.quick_prompt(
                    screen, 'replace [y(es), n(o), a(ll)]?',
                    frozenset('yna'), resize=highlight,
                )
            if res in {'y', 'a'}:
                count += 1
                with self.edit_action_context('replace', final=True):
                    replaced = match.expand(replace)
                    line = screen.file.lines[line_y]
                    line = line[:match.start()] + replaced + line[match.end():]
                    screen.file.lines[line_y] = line
                    screen.file.modified = True
                search.offset = len(replaced)
            elif res == 'n':
                search.offset = 1
            else:
                assert res is PromptResult.CANCELLED
                return

        if res == '':  # we never went through the loop
            screen.status.update('no matches')
        else:
            occurrences = 'occurrence' if count == 1 else 'occurrences'
            screen.status.update(f'replaced {count} {occurrences}')

    @action
    def page_up(self, margin: Margin) -> None:
        if self.cursor_y < margin.body_lines:
            self.cursor_y = self.file_y = 0
        else:
            pos = max(self.file_y - margin.page_size, 0)
            self.cursor_y = self.file_y = pos
        self._set_x_after_vertical_movement()

    @action
    def page_down(self, margin: Margin) -> None:
        if self.file_y + margin.body_lines >= len(self.lines):
            self.cursor_y = len(self.lines) - 1
        else:
            pos = self.file_y + margin.page_size
            self.cursor_y = self.file_y = pos
        self._set_x_after_vertical_movement()

    # editing

    @edit_action('backspace text', final=False)
    def backspace(self, margin: Margin) -> None:
        # backspace at the beginning of the file does nothing
        if self.cursor_y == 0 and self.x == 0:
            pass
        # at the beginning of the line, we join the current line and
        # the previous line
        elif self.x == 0:
            victim = self.lines.pop(self.cursor_y)
            new_x = len(self.lines[self.cursor_y - 1])
            self.lines[self.cursor_y - 1] += victim
            self._decrement_y(margin)
            self.x = self.x_hint = new_x
            # deleting the fake end-of-file doesn't cause modification
            self.modified |= self.cursor_y < len(self.lines) - 1
            _restore_lines_eof_invariant(self.lines)
        else:
            s = self.lines[self.cursor_y]
            self.lines[self.cursor_y] = s[:self.x - 1] + s[self.x:]
            self.x = self.x_hint = self.x - 1
            self.modified = True

    @edit_action('delete text', final=False)
    def delete(self, margin: Margin) -> None:
        # noop at end of the file
        if self.cursor_y == len(self.lines) - 1:
            pass
        # if we're at the end of the line, collapse the line afterwards
        elif self.x == len(self.lines[self.cursor_y]):
            victim = self.lines.pop(self.cursor_y + 1)
            self.lines[self.cursor_y] += victim
            self.modified = True
        else:
            s = self.lines[self.cursor_y]
            self.lines[self.cursor_y] = s[:self.x] + s[self.x + 1:]
            self.modified = True

    @edit_action('line break', final=False)
    def enter(self, margin: Margin) -> None:
        s = self.lines[self.cursor_y]
        self.lines[self.cursor_y] = s[:self.x]
        self.lines.insert(self.cursor_y + 1, s[self.x:])
        self._increment_y(margin)
        self.x = self.x_hint = 0
        self.modified = True

    @edit_action('cut', final=False)
    def cut(self, cut_buffer: Tuple[str, ...]) -> Tuple[str, ...]:
        if self.cursor_y == len(self.lines) - 1:
            return ()
        else:
            victim = self.lines.pop(self.cursor_y)
            self.x = self.x_hint = 0
            self.modified = True
            return cut_buffer + (victim,)

    @edit_action('uncut', final=True)
    def uncut(self, cut_buffer: Tuple[str, ...], margin: Margin) -> None:
        for cut_line in cut_buffer:
            line = self.lines[self.cursor_y]
            before, after = line[:self.x], line[self.x:]
            self.lines[self.cursor_y] = before + cut_line
            self.lines.insert(self.cursor_y + 1, after)
            self._increment_y(margin)
            self.x = self.x_hint = 0

    DISPATCH = {
        # movement
        curses.KEY_DOWN: down,
        curses.KEY_UP: up,
        curses.KEY_LEFT: left,
        curses.KEY_RIGHT: right,
        curses.KEY_HOME: home,
        curses.KEY_END: end,
        curses.KEY_PPAGE: page_up,
        curses.KEY_NPAGE: page_down,
        # editing
        curses.KEY_BACKSPACE: backspace,
        curses.KEY_DC: delete,
        ord('\r'): enter,
    }
    DISPATCH_KEY = {
        # movement
        b'^A': home,
        b'^E': end,
        b'^Y': page_up,
        b'^V': page_down,
        b'kHOM5': ctrl_home,
        b'kEND5': ctrl_end,
        b'kUP5': ctrl_up,
        b'kDN5': ctrl_down,
        b'kRIT5': ctrl_right,
        b'kLFT5': ctrl_left,
    }

    @edit_action('text', final=False)
    def c(self, wch: str, margin: Margin) -> None:
        s = self.lines[self.cursor_y]
        self.lines[self.cursor_y] = s[:self.x] + wch + s[self.x:]
        self.x = self.x_hint = self.x + 1
        self.modified = True
        _restore_lines_eof_invariant(self.lines)

    def mark_previous_action_as_final(self) -> None:
        if self.undo_stack:
            self.undo_stack[-1].final = True

    @contextlib.contextmanager
    def edit_action_context(
            self, name: str,
            *,
            final: bool,
    ) -> Generator[None, None, None]:
        continue_last = (
            self.undo_stack and
            self.undo_stack[-1].name == name and
            not self.undo_stack[-1].final
        )
        if continue_last:
            spy = self.undo_stack[-1].spy
        else:
            if self.undo_stack:
                self.undo_stack[-1].final = True
            spy = ListSpy(self.lines)

        before_x, before_line = self.x, self.cursor_y
        before_modified = self.modified
        assert not isinstance(self.lines, ListSpy), 'recursive action?'
        orig, self.lines = self.lines, spy
        try:
            yield
        finally:
            self.lines = orig
            self.redo_stack.clear()
            if continue_last:
                self.undo_stack[-1].end_x = self.x
                self.undo_stack[-1].end_y = self.cursor_y
                self.undo_stack[-1].end_modified = self.modified
            elif spy.has_modifications:
                action = Action(
                    name=name, spy=spy,
                    start_x=before_x, start_y=before_line,
                    start_modified=before_modified,
                    end_x=self.x, end_y=self.cursor_y,
                    end_modified=self.modified,
                    final=final,
                )
                self.undo_stack.append(action)

    def _undo_redo(
            self,
            op: str,
            from_stack: List[Action],
            to_stack: List[Action],
            status: Status,
            margin: Margin,
    ) -> None:
        if not from_stack:
            status.update(f'nothing to {op}!')
        else:
            action = from_stack.pop()
            to_stack.append(action.apply(self))
            self.scroll_screen_if_needed(margin)
            status.update(f'{op}: {action.name}')

    def undo(self, status: Status, margin: Margin) -> None:
        self._undo_redo(
            'undo', self.undo_stack, self.redo_stack, status, margin,
        )

    def redo(self, status: Status, margin: Margin) -> None:
        self._undo_redo(
            'redo', self.redo_stack, self.undo_stack, status, margin,
        )

    # positioning

    def rendered_y(self, margin: Margin) -> int:
        return self.cursor_y - self.file_y + margin.header

    def rendered_x(self) -> int:
        return self.x - _line_x(self.x, curses.COLS)

    def move_cursor(
            self,
            stdscr: 'curses._CursesWindow',
            margin: Margin,
    ) -> None:
        stdscr.move(self.rendered_y(margin), self.rendered_x())

    def draw(self, stdscr: 'curses._CursesWindow', margin: Margin) -> None:
        to_display = min(len(self.lines) - self.file_y, margin.body_lines)
        for i in range(to_display):
            line_idx = self.file_y + i
            line = self.lines[line_idx]
            current = line_idx == self.cursor_y
            line = _scrolled_line(line, self.x, curses.COLS, current=current)
            stdscr.insstr(i + margin.header, 0, line)
        blankline = ' ' * curses.COLS
        for i in range(to_display, margin.body_lines):
            stdscr.insstr(i + margin.header, 0, blankline)

    def current_position(self, status: Status) -> None:
        line = f'line {self.cursor_y + 1}'
        col = f'col {self.x + 1}'
        line_count = max(len(self.lines) - 1, 1)
        lines_word = 'line' if line_count == 1 else 'lines'
        status.update(f'{line}, {col} (of {line_count} {lines_word})')


class Screen:
    def __init__(
            self,
            stdscr: 'curses._CursesWindow',
            files: List[File],
    ) -> None:
        self.stdscr = stdscr
        self.files = files
        self.i = 0
        self.status = Status()
        self.margin = Margin.from_screen(self.stdscr)
        self.cut_buffer: Tuple[str, ...] = ()

    @property
    def file(self) -> File:
        return self.files[self.i]

    def _draw_header(self) -> None:
        filename = self.file.filename or '<<new file>>'
        if self.file.modified:
            filename += ' *'
        if len(self.files) > 1:
            files = f'[{self.i + 1}/{len(self.files)}] '
            version_width = len(VERSION_STR) + 2 + len(files)
        else:
            files = ''
            version_width = len(VERSION_STR) + 2
        centered = filename.center(curses.COLS)[version_width:]
        s = f' {VERSION_STR} {files}{centered}{files}'
        self.stdscr.insstr(0, 0, s, curses.A_REVERSE)

    def draw(self) -> None:
        if self.margin.header:
            self._draw_header()
        self.file.draw(self.stdscr, self.margin)
        self.status.draw(self.stdscr, self.margin)

    def resize(self) -> None:
        curses.update_lines_cols()
        self.margin = Margin.from_screen(self.stdscr)
        self.file.scroll_screen_if_needed(self.margin)
        self.draw()


def _color_test(stdscr: 'curses._CursesWindow') -> None:
    header = f' {VERSION_STR}'
    header += '<< color test >>'.center(curses.COLS)[len(header):]
    stdscr.insstr(0, 0, header, curses.A_REVERSE)

    maxy, maxx = stdscr.getmaxyx()
    if maxy < 19 or maxx < 68:  # pragma: no cover (will be deleted)
        raise SystemExit('--color-test needs a window of at least 68 x 19')

    y = 1
    for fg in range(-1, 16):
        x = 0
        for bg in range(-1, 16):
            if bg > fg:
                s = f'*{COLORS[bg, fg]:3}'
            else:
                s = f' {COLORS[fg, bg]:3}'
            stdscr.addstr(y, x, s, _color(fg, bg))
            x += 4
        y += 1
    stdscr.get_wch()


class Key(NamedTuple):
    wch: Union[int, str]
    key: int
    keyname: bytes


# TODO: find a place to populate these, surely there's a database somewhere
SEQUENCE_KEY = {
    '\x1bOH': curses.KEY_HOME,
    '\x1bOF': curses.KEY_END,
}
SEQUENCE_KEYNAME = {
    '\x1b[1;5H': b'kHOM5',  # ^Home
    '\x1b[1;5F': b'kEND5',  # ^End
    '\x1bOH': b'KEY_HOME',
    '\x1bOF': b'KEY_END',
    '\x1b[1;3A': b'kUP3',  # M-Up
    '\x1b[1;3B': b'kDN3',  # M-Down
    '\x1b[1;3C': b'kRIT3',  # M-Right
    '\x1b[1;3D': b'kLFT3',  # M-Left
    '\x1b[1;5A': b'kUP5',  # ^Up
    '\x1b[1;5B': b'kDN5',  # ^Down
    '\x1b[1;5C': b'kRIT5',  # ^Right
    '\x1b[1;5D': b'kLFT5',  # ^Left
}


def _get_char(stdscr: 'curses._CursesWindow') -> Key:
    wch = stdscr.get_wch()
    if isinstance(wch, str) and wch == '\x1b':
        stdscr.nodelay(True)
        try:
            while True:
                try:
                    new_wch = stdscr.get_wch()
                    if isinstance(new_wch, str):
                        wch += new_wch
                    else:  # pragma: no cover (impossible?)
                        curses.unget_wch(new_wch)
                        break
                except curses.error:
                    break
        finally:
            stdscr.nodelay(False)

        if len(wch) == 2:
            return Key(wch, -1, f'M-{wch[1]}'.encode())
        elif len(wch) > 1:
            key = SEQUENCE_KEY.get(wch, -1)
            keyname = SEQUENCE_KEYNAME.get(wch, b'unknown')
            return Key(wch, key, keyname)
    elif wch == '\x7f':  # pragma: no cover (macos)
        key = curses.KEY_BACKSPACE
        keyname = curses.keyname(key)
        return Key(wch, key, keyname)

    key = wch if isinstance(wch, int) else ord(wch)
    keyname = curses.keyname(key)
    return Key(wch, key, keyname)


def _save(screen: Screen) -> Optional[PromptResult]:
    screen.file.mark_previous_action_as_final()

    # TODO: make directories if they don't exist
    # TODO: maybe use mtime / stat as a shortcut for hashing below
    # TODO: strip trailing whitespace?
    # TODO: save atomically?
    if screen.file.filename is None:
        filename = screen.status.prompt(screen, 'enter filename')
        if filename is PromptResult.CANCELLED:
            return PromptResult.CANCELLED
        else:
            screen.file.filename = filename

    if os.path.isfile(screen.file.filename):
        with open(screen.file.filename) as f:
            *_, sha256 = _get_lines(f)
    else:
        sha256 = hashlib.sha256(b'').hexdigest()

    contents = screen.file.nl.join(screen.file.lines)
    sha256_to_save = hashlib.sha256(contents.encode()).hexdigest()

    # the file on disk is the same as when we opened it
    if sha256 not in (screen.file.sha256, sha256_to_save):
        screen.status.update('(file changed on disk, not implemented)')
        return PromptResult.CANCELLED

    with open(screen.file.filename, 'w') as f:
        f.write(contents)

    screen.file.modified = False
    screen.file.sha256 = sha256_to_save
    num_lines = len(screen.file.lines) - 1
    lines = 'lines' if num_lines != 1 else 'line'
    screen.status.update(f'saved! ({num_lines} {lines} written)')

    # fix up modified state in undo / redo stacks
    for stack in (screen.file.undo_stack, screen.file.redo_stack):
        first = True
        for action in reversed(stack):
            action.end_modified = not first
            action.start_modified = True
            first = False
    return None


def _save_filename(screen: Screen) -> Optional[PromptResult]:
    response = screen.status.prompt(
        screen, 'enter filename', default=screen.file.filename,
    )
    if response is PromptResult.CANCELLED:
        return PromptResult.CANCELLED
    else:
        screen.file.filename = response
        return _save(screen)


def _quit(screen: Screen) -> Optional[EditResult]:
    if screen.file.modified:
        response = screen.status.quick_prompt(
            screen, 'file is modified - save [y(es), n(o)]?', frozenset('yn'),
        )
        if response == 'y':
            if _save_filename(screen) is not PromptResult.CANCELLED:
                return EditResult.EXIT
            else:
                return None
        elif response == 'n':
            return EditResult.EXIT
        else:
            assert response is PromptResult.CANCELLED
            return None
    return EditResult.EXIT


ScreenFunc = Callable[[Screen], Union[None, PromptResult, EditResult]]
DISPATCH: Dict[bytes, ScreenFunc] = {
    b'^S': _save,
    b'^O': _save_filename,
    b'^X': _quit,
    b'kLFT3': lambda screen: EditResult.PREV,
    b'kRIT3': lambda screen: EditResult.NEXT,
}


def _edit(screen: Screen) -> EditResult:
    prevkey = Key('', 0, b'')
    screen.file.ensure_loaded(screen.status)

    while True:
        screen.status.tick(screen.margin)

        screen.draw()
        screen.file.move_cursor(screen.stdscr, screen.margin)

        key = _get_char(screen.stdscr)

        if key.key == curses.KEY_RESIZE:
            screen.resize()
        elif key.key in File.DISPATCH:
            screen.file.DISPATCH[key.key](screen.file, screen.margin)
        elif key.keyname in File.DISPATCH_KEY:
            screen.file.DISPATCH_KEY[key.keyname](screen.file, screen.margin)
        elif key.keyname == b'^K':
            if prevkey.keyname == b'^K':
                cut_buffer = screen.cut_buffer
            else:
                cut_buffer = ()
            screen.cut_buffer = screen.file.cut(cut_buffer)
        elif key.keyname == b'^U':
            screen.file.uncut(screen.cut_buffer, screen.margin)
        elif key.keyname == b'M-u':
            screen.file.undo(screen.status, screen.margin)
        elif key.keyname == b'M-U':
            screen.file.redo(screen.status, screen.margin)
        elif key.keyname == b'^_':
            response = screen.status.prompt(screen, 'enter line number')
            if response is not PromptResult.CANCELLED:
                try:
                    lineno = int(response)
                except ValueError:
                    screen.status.update(f'not an integer: {response!r}')
                else:
                    screen.file.go_to_line(lineno, screen.margin)
        elif key.keyname == b'^W':
            response = screen.status.prompt(
                screen, 'search', history='search', default_prev=True,
            )
            if response is not PromptResult.CANCELLED:
                try:
                    regex = re.compile(response)
                except re.error:
                    screen.status.update(f'invalid regex: {response!r}')
                else:
                    screen.file.search(regex, screen.status, screen.margin)
        elif key.keyname == b'^\\':
            response = screen.status.prompt(
                screen, 'search (to replace)',
                history='search', default_prev=True,
            )
            if response is not PromptResult.CANCELLED:
                try:
                    regex = re.compile(response)
                except re.error:
                    screen.status.update(f'invalid regex: {response!r}')
                else:
                    response = screen.status.prompt(
                        screen, 'replace with', history='replace',
                        allow_empty=True,
                    )
                    if response is not PromptResult.CANCELLED:
                        screen.file.replace(screen, regex, response)
        elif key.keyname == b'^C':
            screen.file.current_position(screen.status)
        elif key.keyname == b'^[':  # escape
            response = screen.status.prompt(screen, '', history='command')
            if response == ':q':
                return EditResult.EXIT
            elif response == ':w':
                _save(screen)
            elif response == ':wq':
                _save(screen)
                return EditResult.EXIT
            elif response is not PromptResult.CANCELLED:
                screen.status.update(f'invalid command: {response}')
        elif key.keyname in DISPATCH:
            fn_res = DISPATCH[key.keyname](screen)
            if isinstance(fn_res, EditResult):
                return fn_res
        elif key.keyname == b'^Z':
            curses.endwin()
            os.kill(os.getpid(), signal.SIGSTOP)
            screen.stdscr = _init_screen()
            screen.resize()
        elif isinstance(key.wch, str) and key.wch.isprintable():
            screen.file.c(key.wch, screen.margin)
        else:
            screen.status.update(f'unknown key: {key}')

        prevkey = key


def c_main(stdscr: 'curses._CursesWindow', args: argparse.Namespace) -> None:
    if args.color_test:
        return _color_test(stdscr)
    screen = Screen(stdscr, [File(f) for f in args.filenames or [None]])
    with screen.status.save_history():
        while screen.files:
            screen.i = screen.i % len(screen.files)
            res = _edit(screen)
            if res == EditResult.EXIT:
                del screen.files[screen.i]
                screen.status.clear()
            elif res == EditResult.NEXT:
                screen.i += 1
                screen.status.clear()
            elif res == EditResult.PREV:
                screen.i -= 1
                screen.status.clear()
            else:
                raise AssertionError(f'unreachable {res}')


def _init_screen() -> 'curses._CursesWindow':
    # set the escape delay so curses does not pause waiting for sequences
    if sys.version_info >= (3, 9):  # pragma: no cover
        curses.set_escdelay(25)
    else:  # pragma: no cover
        os.environ.setdefault('ESCDELAY', '25')

    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    # <enter> is not transformed into '\n' so it can be differentiated from ^J
    curses.nonl()
    # ^S / ^Q / ^Z / ^\ are passed through
    curses.raw()
    stdscr.keypad(True)
    with contextlib.suppress(curses.error):
        curses.start_color()
    _init_colors(stdscr)
    return stdscr


@contextlib.contextmanager
def make_stdscr() -> Generator['curses._CursesWindow', None, None]:
    """essentially `curses.wrapper` but split out to implement ^Z"""
    stdscr = _init_screen()
    try:
        yield stdscr
    finally:
        curses.endwin()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--color-test', action='store_true')
    parser.add_argument('filenames', metavar='filename', nargs='*')
    args = parser.parse_args()
    with make_stdscr() as stdscr:
        c_main(stdscr, args)
    return 0


if __name__ == '__main__':
    exit(main())
