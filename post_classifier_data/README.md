## Post Classifier Dataset

This folder is used to train a lightweight (CPU-fast) classifier that decides whether
an image is a "post screenshot worth capturing" or not.

### Folder structure

- `positive/`: images that **ARE** valid post screenshots (bài viết cần chụp)
- `negative/`: images that **ARE NOT** valid post screenshots (không phải bài viết cần chụp)

This project supports **two modes**:

- **Binary (positive+negative)**: when you have both folders, the model learns to predict **P(positive)**.
- **Reject-only (negative-only)**: if you only have `negative/`, the model rejects only when very sure.

### Supported files

Common image formats: `.png`, `.jpg`, `.jpeg`, `.webp`.

### Train

Run from the repo root:

```bash
python -m app.worker.post_classifier.train
```

This will create/update:

- `app/worker/post_classifier/model.json`

### Notes

- Recommended: collect at least **≥20 images** per class for binary mode.

