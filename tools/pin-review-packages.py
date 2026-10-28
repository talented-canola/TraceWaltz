from pathlib import Path
import shutil
project = Path(__file__).resolve().parents[1]
root = project / '.runtime/linux-review'
root.mkdir(parents=True, exist_ok=True)
shutil.copyfile(project / 'config/review-package.json', root / 'package.json')
print('Pinned review dependency roster prepared.')
