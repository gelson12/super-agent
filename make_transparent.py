"""
Pure-stdlib PNG white-background removal.
Usage: python make_transparent.py <input.png> <output.png> [threshold]
"""
import struct, zlib, sys

def make_transparent(src, dst, threshold=230):
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
    bit_depth  = ihdr[8]
    color_type = ihdr[9]   # 2=RGB, 6=RGBA

    if color_type == 6:
        # Already RGBA — just re-run transparency pass on existing alpha
        pass

    # Collect and decompress IDAT
    idat_raw = b''.join(cd for ct, cd in chunks if ct == b'IDAT')
    raw = bytearray(zlib.decompress(idat_raw))

    if color_type == 2:
        bpp = 3
    elif color_type == 6:
        bpp = 4
    else:
        raise ValueError(f"Unsupported color_type {color_type}")

    stride = 1 + width * bpp
    new_raw = bytearray()

    for row in range(height):
        base = row * stride
        new_raw.append(raw[base])  # filter byte
        for col in range(width):
            px = base + 1 + col * bpp
            r, g, b = raw[px], raw[px+1], raw[px+2]
            a = raw[px+3] if bpp == 4 else 255
            # Make near-white pixels transparent
            if r > threshold and g > threshold and b > threshold:
                a = 0
            elif r > threshold - 20 and g > threshold - 20 and b > threshold - 20:
                # Soft feather edge
                whiteness = (r + g + b) / 3
                a = int((1 - (whiteness - (threshold - 20)) / 20) * a)
            new_raw += bytes([r, g, b, a])

    # New IHDR: force color_type=6 (RGBA)
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
    print(f"Saved transparent PNG: {dst} ({width}x{height})")

if __name__ == '__main__':
    src = sys.argv[1]
    dst = sys.argv[2]
    thr = int(sys.argv[3]) if len(sys.argv) > 3 else 230
    make_transparent(src, dst, thr)
