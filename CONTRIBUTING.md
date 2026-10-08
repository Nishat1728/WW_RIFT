# Contributing

Thank you for improving RIFT Input Builder. Contributions should preserve the workflow's central
contract: selected native products are saved unchanged before processing, and no product is marked
accepted until its quality checks pass.

## Development setup

```bash
conda env create -f environment.yml
conda activate riftprep
pytest
python tools/build_general_notebook.py
git diff --exit-code -- RIFT_Input_Builder.ipynb
```

Use a small local fixture for unit tests. Do not commit provider downloads, credentials, caches,
generated case directories, or benchmark outputs.

## Adding a source

1. Add the provider and its status to both source-catalog files.
2. Implement raw download, normalization, provenance, QC, and plotting in the backend.
3. Preserve the provider response before any conversion.
4. Add mocked unit tests and a small live-provider smoke test.
5. Expose the source in the notebook only after the complete adapter is executable.
6. Regenerate the notebook and update the README.

## Pull requests

Describe the scientific assumption, supported geographic/time coverage, credentials or licences,
raw artifacts, transformations, QC rules, and tests. Keep unrelated changes separate. By
contributing, you agree that your contribution is licensed under the repository's BSD 3-Clause
license.
