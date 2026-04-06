"""
PNG background removal using flood-fill from all border pixels.
Handles any shade of neutral grey/white checkerboard background.
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
        # Near-neutral (low colour variance) AND not very dark.
        # Threshold 80 catches white (255) and any checkerboard grey down to ~80.
        # Variance check 50 excludes gold, steel-blue and other logo colours.
        brightness = (int(r) + int(g) + int(b)) / 3
        variance = max(abs(int(r) - int(g)), abs(int(g) - int(b)), abs(int(r) - int(b)))
        return brightness > 80 and variance < 50

    visited = bytearray(width * height)
    queue = deque()

    def seed(x, y):
        idx = y * width + x
        if not visited[idx] and is_bg(*pixels[x, y]):
            visited[idx] = 1
            queue.append((x, y))

    # Seed from ALL four edges, not just four corners
    for x in range(width):
        seed(x, 0)
        seed(x, height - 1)
    for y in range(height):
        seed(0, y)
        seed(width - 1, y)

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
    print(f"Done: {dst} ({width}x{height}, RGBA, background removed)")


if __name__ == '__main__':
    make_transparent(sys.argv[1], sys.argv[2])
