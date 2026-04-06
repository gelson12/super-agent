"""
PNG background removal using flood-fill from corners.
Handles checkered (white + grey) transparency backgrounds.
Usage: python make_transparent.py <input.png> <output.png>
"""
from PIL import Image
import sys
from collections import deque


def make_transparent(src, dst):
    img = Image.open(src).convert('RGBA')
    width, height = img.size
    pixels = img.load()

    def is_bg(r, g, b, a):
        # Match white AND checkerboard grey squares (~204,204,204)
        return (r > 175 and g > 175 and b > 175
                and abs(int(r) - int(g)) < 30
                and abs(int(g) - int(b)) < 30)

    visited = bytearray(width * height)
    queue = deque()

    for sx, sy in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]:
        if not visited[sy * width + sx] and is_bg(*pixels[sx, sy]):
            queue.append((sx, sy))
            visited[sy * width + sx] = 1

    while queue:
        x, y = queue.popleft()
        r, g, b, a = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height:
                idx = ny * width + nx
                if not visited[idx]:
                    visited[idx] = 1
                    if is_bg(*pixels[nx, ny]):
                        queue.append((nx, ny))

    img.save(dst, 'PNG')
    print(f"Done: {dst} ({width}x{height}, RGBA, flood-fill background removed)")


if __name__ == '__main__':
    make_transparent(sys.argv[1], sys.argv[2])
