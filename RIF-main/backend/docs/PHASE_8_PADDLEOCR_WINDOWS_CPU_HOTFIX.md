# Phase 8 PaddleOCR Windows CPU Hotfix

Some Windows CPU setups using PaddlePaddle 3.3.x and PaddleOCR 3.x can raise
`ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`
during OCR prediction through the oneDNN/PIR runtime.

This hotfix updates the local PaddleOCR adapter only. Immediately before
PaddleOCR imports PaddlePaddle, it disables the affected optional execution flags:

- `FLAGS_use_mkldnn=0`
- `PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0`
- `FLAGS_enable_pir_api=0`

The OCR pipeline remains fully local. No cloud OCR service is used.

Expected result after applying the hotfix: JPG/PNG OCR should return
`paddleocr_image` with `ocr_used: true`.
