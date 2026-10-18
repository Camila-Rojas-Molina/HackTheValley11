# Demo bundle: attribution and labels

Research prototype on public, de-identified data. Not a medical device. Not for clinical use or diagnosis.

This folder holds the small files the RainbowBone demo loads: two real CT crops of one vertebra and one synthetic phantom. Each file's sha256, source script and provenance are listed in `PROVENANCE.json`.

## Real data: TCIA Spinal-Multiple-Myeloma-SEG (CC BY 4.0)

Files: everything in `myel018-L1-d273/` and `myel018-L1-d0/`.

> Nohel M, Válek V, Rohan T, Štork M, Jakubíček R, Chmelík J, Dostál M (2026). Spinal-Multiple-Myeloma-SEG (Version 1). The Cancer Imaging Archive. DOI [10.7937/k4qv-hh78](https://doi.org/10.7937/k4qv-hh78). Licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

- Used under CC BY 4.0 and the TCIA Data Usage Policy. Downloaded from the NCI Imaging Data Commons with `idc-index`.
- Case Myel_018, vertebra L1, conventional 120 kVp CT. The two scans are shown as **Day 0** and **Day 273**, the interval recorded in the de-identified data.
- **What we changed:**
  - DICOM CT was converted to NIfTI. The dataset's DICOM-SEG vertebra and lesion segmentations were converted to label maps on the CT grid by physical coordinates.
  - Each scan was cropped to L1 with a 10 mm margin and resampled to 1 mm isotropic voxels: linear interpolation for the CT, nearest-neighbour for the masks.
  - The lesion mask was restricted to the L1 vertebra mask.
- **Masks:** both masks come from the dataset. Outlines read "outline: dataset radiologist-corrected mask". We did not draw them; the resampling and the restriction above are the only changes.
- **The two crops are not registered.** They sit on different grids (95×109×80 vs 95×110×80 voxels, different origins). Registration arrives in M5.

| Folder | Scan | Files |
|---|---|---|
| `myel018-L1-d273/` | Day 273 (scan B) | `ct.nii.gz` (int16 HU), `mask_vertebra.expert.nii.gz`, `mask_lesion.expert.nii.gz` (uint8) |
| `myel018-L1-d0/` | Day 0 (scan A) | the same three files. The lesion mask is empty, because L1 has no lesion on Day 0 |

**Lesion volumes name their grid:**
- Day 273, L1 lesion: 3.24 cm³ (original scan grid; dataset radiologist-corrected mask).
- The same lesion on the 1 mm crop: 3,217 voxels, which is 3.22 cm³.
- Vertebra volume (incl. lesion): 68.52 cm³ on the original grid and 67.79 cm³ on the crop.
- Day 0: lesion 0.00 cm³. Vertebra volume (incl. lesion): 69.24 cm³ on the original grid and 69.38 cm³ on the crop.

**Not in this bundle yet:**
- the stiffness numbers ("pending FE (M5)");
- the calibration block (`null` in `PROVENANCE.json` until M5);
- strain fields for the real case.

## Synthetic phantom: MOCK, not patient data

Files: everything in `phantom/`. Label: **"Synthetic phantom · mock strain field"**.

- `ct.nii.gz` is a synthetic femur-shaft CT made by our own script: a tube with a cortical shell, marrow and a spherical lytic defect, with seeded noise. It contains no third-party or patient data.
- `strain_min_principal_ue.mock.nii.gz` (a volume on the phantom grid) and `strain_min_principal_ue.mock.mz3` (a surface mesh) are **MOCK**.
  - Their values come from a fixed formula, not from a finite-element simulation.
  - They lie between 0 and 1500 microstrain, so the viewer's strain display can be shown before the physics exists.
  - Wherever they are shown, the legend must read "Compressive strain (µε) · MOCK".
- **A mock strain field is never shown on real anatomy.** The real crops carry no strain field until the M5 physics produces one.
