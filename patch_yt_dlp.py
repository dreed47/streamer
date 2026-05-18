import pathlib

p = pathlib.Path('/usr/local/lib/python3.12/site-packages/yt_dlp/extractor/stripchat.py')
src = p.read_text()
old = "if traverse_obj(data, ('viewCam', 'show', {dict})):"
new = "if traverse_obj(data, ('viewCam', 'show', 'type')) in ('private', 'p2p'):"
assert old in src, f'patch target not found — yt-dlp version may have changed'
p.write_text(src.replace(old, new))
print('patched stripchat.py')
