import base64
import lzma
import textwrap

# Ask the user which file to compress
file_path = input("Enter the path to the file you want to compress: ")
with open(file_path, "rb") as f:
	SOURCE = f.read()

# Try all standard and PRESET_EXTREME presets.
# Prefer the smallest output, then the lowest preset level, then non-extreme.
best_key = None
best_preset = None
best_is_extreme = False
blob = b""
for preset in range(10):
	for is_extreme in (False, True):
		preset_value = preset | (lzma.PRESET_EXTREME if is_extreme else 0)
		candidate = lzma.compress(SOURCE, preset=preset_value)
		candidate_key = (len(candidate), preset, is_extreme)
		if best_key is None or candidate_key < best_key:
			best_key = candidate_key
			best_preset = preset
			best_is_extreme = is_extreme
			blob = candidate

b85 = base64.a85encode(blob)

# Wrap at <= 80 columns so it’s editor-friendly
b85 = b85.decode('ascii')
b85 = "\n".join(textwrap.wrap(b85, width=116,expand_tabs=False,replace_whitespace=False,drop_whitespace=False,break_on_hyphens=False))

print(b85)
selected_name = f"{best_preset}{' | PRESET_EXTREME' if best_is_extreme else ''}"
print(f"\nSelected LZMA preset: {selected_name}")
print(f"\nOriginal size: {len(SOURCE)} bytes")
print(f"Compressed size: {len(blob)} bytes")