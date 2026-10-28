from pathlib import Path
import os
import shutil
import sys


def link_modules(source, modules):
    source, modules = Path(source).resolve(), Path(modules).resolve()
    if not (source / 'harness/plugins/orchestrator.mjs').is_file() or not modules.is_dir():
        raise RuntimeError('Prepare the EvoTrace source and review dependencies before linking.')
    destination = source / 'node_modules'
    if destination.is_symlink() and destination.resolve() == modules:
        return destination
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('Existing EvoTrace node_modules preserved; use a fresh managed source or check its dependencies explicitly.')
    destination.symlink_to(modules, target_is_directory=True)
    return destination


def main():
    project = Path(__file__).resolve().parents[1]
    root = project / '.runtime/linux-review'
    if '--link' in sys.argv:
        source = Path(os.environ.get('EVOTRACE_SOURCE') or project / '.runtime/evotrace-source')
        link_modules(source, root / 'node_modules')
        print('EvoTrace plugins linked to the pinned review dependencies.')
    else:
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / 'config/review-package.json', root / 'package.json')
        print('Pinned review dependency roster prepared.')


if __name__ == '__main__':
    main()
