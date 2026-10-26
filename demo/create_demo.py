"""Create a demonstration ZIP that contains made-up, non-personal data."""
from pathlib import Path
from zipfile import ZipFile
root=Path(__file__).resolve().parent
out=root/'sample_archive.zip'
with ZipFile(out,'w') as z:
    z.write(root/'comments0.html',arcname='archive/comments/comments0.html')
print(out)
