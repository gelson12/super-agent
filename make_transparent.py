"""
PNG background removal using flood-fill from corners.
Handles checkered (white + grey) transparency backgrounds.
Usage: python make_transparent.py <input.png> <output.png>
"""
import struct, zlib, sys
from collections import deque

def make_transparent(src, dst):
    with open(src, 'rb') as f:
        data = f.read()

    assert data[:8] == b'\x89PNG\r\n\x1a\n', "Not a PNG"

    chunks = []
    i = 8
    while i < len(data):
        length = struct.unpack('>I', data[i:i+4])[0]
        ctype  = data[i+4:i+8]
        cdata  = data[i+8:i+8+length]
        chunks.append((ctype, cdata))
        i += 12 + length

    ihdr = chunks[0][1]
    width      = struct.unpack('>I', ihdr[0:4])[0]
    height     = struct.unpack('>I', ihdr[4:8])[0]
    color_type = ihdr[9]   # 2=RGB, 6=RGBA

    idat_raw = b''.join(cd for ct, cd in chunks if ct == b'IDAT')
    raw = bytearray(zlib.decompress(idat_raw))

    bpp = 3 if color_type == 2 else 4
    stride = 1 + width * bpp

    def get_px(x, y):
        base = y * stride + 1 + x * bpp
        return raw[base], raw[base+1], raw[base+2]

    def is_bg(r, g, b):
        # Match white AND checkerboard grey squares (~204,204,204)
        return r > 175 and g > 175 and b > 175 and abs(int(r)-int(g)) < 30 and abs(int(g)-int(b)) < 30

    # Flood fill from all four corners to find background pixels
    visited = bytearray(width * height)  # 0=unvisited, 1=background
    queue = deque()
    for (sx, sy) in [(0,0),(width-1,0),(0,height-1),(width-1,height-1)]:
        if not visited[sy*width+sx] and is_bg(*get_px(sx, sy)):
            queue.append((sx, sy))
            visited[sy*width+sx] = 1

    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nx, ny = x+dx, y+dy
            if 0 <= nx < width and 0 <= ny < height:
                idx = ny*width+nx
                if not visited[idx] and is_bg(*get_px(nx, ny)):
                    visited[idx] = 1
                    queue.append((nx, ny))

    # Build RGBA output
    new_raw = bytearray()
    for y in range(height):
        base = y * stride
        new_raw.append(raw[base])  # filter byte
        for x in range(width):
            px = base + 1 + x * bpp
            r, g, b = raw[px], raw[px+1], raw[px+2]
            a = 0 if visited[y*width+x] else 255
            new_raw += bytes([r, g, b, a])

    new_ihdr = ihdr[:9] + b'\x06' + ihdr[10:]
    new_idat = zlib.compress(bytes(new_raw), 9)

    def chunk(ct, cd):
        crc = zlib.crc32(ct + cd) & 0xffffffff
        return struct.pack('>I', len(cd)) + ct + cd + struct.pack('>I', crc)

    out = b'\x89PNG\r\n\x1a\n'
    for ct, cd in chunks:
        if ct == b'IDAT':
            continue
        elif ct == b'IHDR':
            out += chunk(b'IHDR', new_ihdr)
        elif ct == b'IEND':
            out += chunk(b'IDAT', new_idat)
            out += chunk(b'IEND', b'')
        else:
            out += chunk(ct, cd)

    with open(dst, 'wb') as f:
        f.write(out)
    print(f"Done: {dst} ({width}x{height}, RGBA, flood-fill background removed)")

if __name__ == '__main__':
    make_transparent(sys.argv[1], sys.argv[2])
