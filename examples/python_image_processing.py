import io
import tempfile
from pathlib import Path

from PIL import Image

from examples.console import console
from localsandbox import LocalSandbox

PYTHON_CODE = """\
from PIL import Image, ImageFilter

# Load the image from the sandbox filesystem
img = Image.open('/data/receipt.jpg')

# Print original image info
print(f"Original: {img.size[0]}x{img.size[1]}, format={img.format}, mode={img.mode}")

# 1. Resize the image to 50% of original
width, height = img.size
resized = img.resize((width // 2, height // 2))
resized.save('/data/resized.jpg', 'JPEG')
print(f"Resized: {resized.size[0]}x{resized.size[1]}")

# 2. Convert to grayscale
grayscale = img.convert('L')
grayscale.save('/data/grayscale.jpg', 'JPEG')
print(f"Grayscale: mode={grayscale.mode}")

# 3. Crop a 200x200 region from the top-left corner
cropped = img.crop((0, 0, 200, 200))
cropped.save('/data/cropped.jpg', 'JPEG')
print(f"Cropped: {cropped.size[0]}x{cropped.size[1]}")

# 4. Create a thumbnail (preserves aspect ratio)
thumbnail = img.copy()
thumbnail.thumbnail((128, 128))
thumbnail.save('/data/thumbnail.jpg', 'JPEG')
print(f"Thumbnail: {thumbnail.size[0]}x{thumbnail.size[1]}")

# 5. Apply a blur filter
blurred = img.filter(ImageFilter.GaussianBlur(radius=3))
blurred.save('/data/blurred.jpg', 'JPEG')
print(f"Blurred: {blurred.size[0]}x{blurred.size[1]}")

print("\\nAll operations completed successfully!")
"""

# Images we generated in the sandbox
GENERATED_IMAGES = [
    "resized.jpg",
    "grayscale.jpg",
    "cropped.jpg",
    "thumbnail.jpg",
    "blurred.jpg",
]


def main() -> None:
    console.print("[bold blue]LocalSandbox PIL Image Processing Example[/bold blue]")

    # Use the test receipt image
    receipt_path = Path(__file__).parent.parent / "tests" / "data" / "receipt.jpg"
    if not receipt_path.exists():
        console.print(f"[bold red]Test image not found:[/bold red] {receipt_path}")
        console.print("Please ensure tests/data/receipt.jpg exists.")
        return

    console.print(f"\n[green]Input Image:[/green] {receipt_path.name}")

    console.print("\n[green]Python Code to Execute:[/green]")
    console.print_code(PYTHON_CODE.strip())

    with LocalSandbox(files={"/data/receipt.jpg": receipt_path}) as sandbox:
        console.print("\n[yellow]Executing Python code with PIL...[/yellow]")

        result = sandbox.execute_python(
            PYTHON_CODE,
            preload_packages=["pillow"],
        )

        if result.exit_code != 0:
            console.print(f"[bold red]Execution failed:[/bold red]\n{result.stderr}")
            return

        console.print("\n[green]Output:[/green]")
        for line in result.stdout.strip().split("\n"):
            console.print(f"  {line}")

        console.print("\n[yellow]Generated files in sandbox:[/yellow]")
        files = sandbox.list_files("/data")
        for f in sorted(files):
            console.print(f"  - {f}")

        # Read generated images using read_file_bytes and verify them
        console.print(
            "\n[yellow]Reading generated images with read_file_bytes():[/yellow]"
        )
        for filename in GENERATED_IMAGES:
            image_bytes = sandbox.read_file_bytes(f"/data/{filename}")
            img = Image.open(io.BytesIO(image_bytes))
            console.print(
                f"  - {filename}: {len(image_bytes)} bytes, {img.size[0]}x{img.size[1]}"
            )

        # Save images to temp directory for viewing
        output_dir = Path(tempfile.gettempdir()) / "localsandbox_images"
        output_dir.mkdir(exist_ok=True)
        console.print(f"\n[yellow]Saving images to:[/yellow] {output_dir}")
        for filename in GENERATED_IMAGES:
            image_bytes = sandbox.read_file_bytes(f"/data/{filename}")
            output_path = output_dir / filename
            output_path.write_bytes(image_bytes)
            console.print(f"  - Saved: {output_path}")

        console.print("\n[green]Execution History (last 5):[/green]")
        for entry in sandbox.history(limit=5):
            params = entry.parameters or {}
            console.print(f" - [bold]{entry.name}[/bold]: {params}")


if __name__ == "__main__":
    main()
