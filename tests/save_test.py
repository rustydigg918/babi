import pytest

from testing.runner import and_exit
from testing.runner import run


def test_mixed_newlines(tmpdir):
    f = tmpdir.join('f')
    f.write_binary(b'foo\nbar\r\n')
    with run(str(f)) as h, and_exit(h):
        # should start as modified
        h.await_text('f *')
        h.await_text(r"mixed newlines will be converted to '\n'")


def test_new_file():
    with run('this_is_a_new_file') as h, and_exit(h):
        h.await_text('this_is_a_new_file')
        h.await_text('(new file)')


def test_not_a_file(tmpdir):
    d = tmpdir.join('d').ensure_dir()
    with run(str(d)) as h, and_exit(h):
        h.await_text('<<new file>>')
        h.await_text("d' is not a file")


def test_save_no_filename_specified(tmpdir):
    f = tmpdir.join('f')

    with run() as h, and_exit(h):
        h.press('hello world')
        h.press('^S')
        h.await_text('enter filename:')
        h.press_and_enter(str(f))
        h.await_text('saved! (1 line written)')
        h.await_text_missing('*')
        assert f.read() == 'hello world\n'


@pytest.mark.parametrize('k', ('Enter', '^C'))
def test_save_no_filename_specified_cancel(k):
    with run() as h, and_exit(h):
        h.press('hello world')
        h.press('^S')
        h.await_text('enter filename:')
        h.press(k)
        h.await_text('cancelled')


def test_saving_file_on_disk_changes(tmpdir):
    # TODO: this should show some sort of diffing thing or just allow overwrite
    f = tmpdir.join('f')

    with run(str(f)) as h, and_exit(h):
        f.write('hello world')

        h.press('^S')
        h.await_text('file changed on disk, not implemented')


def test_allows_saving_same_contents_as_modified_contents(tmpdir):
    f = tmpdir.join('f')

    with run(str(f)) as h, and_exit(h):
        f.write('hello world\n')
        h.press('hello world')
        h.await_text('hello world')

        h.press('^S')
        h.await_text('saved! (1 line written)')
        h.await_text_missing('*')

    assert f.read() == 'hello world\n'


def test_allows_saving_if_file_on_disk_does_not_change(tmpdir):
    f = tmpdir.join('f')
    f.write('hello world\n')

    with run(str(f)) as h, and_exit(h):
        h.await_text('hello world')
        h.press('ohai')
        h.press('Enter')

        h.press('^S')
        h.await_text('saved! (2 lines written)')
        h.await_text_missing('*')

    assert f.read() == 'ohai\nhello world\n'


def test_save_file_when_it_did_not_exist(tmpdir):
    f = tmpdir.join('f')

    with run(str(f)) as h, and_exit(h):
        h.press('hello world')
        h.press('^S')
        h.await_text('saved! (1 line written)')
        h.await_text_missing('*')

    assert f.read() == 'hello world\n'


def test_save_via_ctrl_o(tmpdir):
    f = tmpdir.join('f')
    with run(str(f)) as h, and_exit(h):
        h.press('hello world')
        h.press('^O')
        h.await_text(f'enter filename: {f}')
        h.press('Enter')
        h.await_text('saved! (1 line written)')
        assert f.read() == 'hello world\n'


def test_save_via_ctrl_o_set_filename(tmpdir):
    f = tmpdir.join('f')
    with run() as h, and_exit(h):
        h.press('hello world')
        h.press('^O')
        h.await_text('enter filename:')
        h.press_and_enter(str(f))
        h.await_text('saved! (1 line written)')
        assert f.read() == 'hello world\n'


@pytest.mark.parametrize('key', ('^C', 'Enter'))
def test_save_via_ctrl_o_cancelled(tmpdir, key):
    with run() as h, and_exit(h):
        h.press('hello world')
        h.press('^O')
        h.await_text('enter filename:')
        h.press(key)
        h.await_text('cancelled')


def test_save_on_exit_cancel_yn():
    with run() as h, and_exit(h):
        h.press('hello')
        h.await_text('hello')
        h.press('^X')
        h.await_text('file is modified - save [y(es), n(o)]?')
        h.press('^C')
        h.await_text('cancelled')


def test_save_on_exit_cancel_filename():
    with run() as h, and_exit(h):
        h.press('hello')
        h.await_text('hello')
        h.press('^X')
        h.await_text('file is modified - save [y(es), n(o)]?')
        h.press('y')
        h.await_text('enter filename:')
        h.press('^C')
        h.await_text('cancelled')


def test_save_on_exit_save(tmpdir):
    f = tmpdir.join('f')
    with run(str(f)) as h:
        h.press('hello')
        h.await_text('hello')
        h.press('^X')
        h.await_text('file is modified - save [y(es), n(o)]?')
        h.press('y')
        h.await_text(f'enter filename: {f}')
        h.press('Enter')
        h.await_exit()
