# builder.py
import base64
import lzma
# import textwrap

# Ask the user which file to compress
file_path = input("Enter the path to the file you want to compress: ")
with open(file_path, "rb") as f:
	SOURCE = f.read()

blob = lzma.compress(SOURCE, preset=9)
b85 = base64.a85encode(blob)

# Wrap at <= 80 columns so it’s editor-friendly
# b85 = b"\n".join(textwrap.wrap(b85, width=76))

print(b85)
print(f"\nOriginal size: {len(SOURCE)} bytes")
print(f"Compressed size: {len(blob)} bytes")