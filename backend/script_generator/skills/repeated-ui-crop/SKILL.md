---
name: repeated-ui-crop
description: >-
  Reliably extract repeated UI regions such as cards, medals, inventory cells,
  buttons, and list rows. Use when several visually related targets need precise
  crops; combine semantic grounding with local fitting, preview verification,
  and atomic batch commit instead of trusting vision-model boxes directly.
---

# Repeated UI Crop

Use visual reasoning to identify *what repeats* and deterministic image tools to
decide *where to cut*. Never treat a vision-model bounding box or confidence as
pixel-accurate ground truth.

## Workflow

1. Call `describe_current_frame(mode=locate)` with the target definition and
   likely confusers. Ask for all visible instances plus shared features: border,
   background, approximate size, arrangement, and stable versus dynamic parts.
2. Pass the returned grounding ids and expected count to
   `fit_repeated_regions`. This performs local multi-threshold contour search,
   duplicate suppression, repeated-size clustering, and consistency scoring.
3. Inspect the returned preview asset with `inspect_asset(mode=verify)`. Verify
   that every numbered rectangle contains exactly one complete target, excludes
   neighbors, and follows the intended stable boundary.
4. Only when verification returns `verified=true`, call
   `commit_repeated_regions(fit_id=...)`. Submission is all-or-none.
5. If verification fails, do not commit and do not retain a batch of provisional
   templates. Refine the visual target or ask the user to mark one representative
   region, then fit again.

## Decisions

- Prefer this workflow for two or more similarly sized rectangular regions.
- Use ordinary `crop_asset` for a single irregular target or an intentional
  free-form user mark.
- Treat partial rows, scroll clipping, overlapping objects, and strongly varying
  sizes as ambiguous. Fit only the complete consistent subset or ask the user.
- A count mismatch, low consistency score, out-of-bounds coordinate, or failed
  preview verification is a stopping condition—not permission to guess.
- Keep the original screenshot as the fitting source. Never fit the next region
  from a previous crop.

## Cost control

One semantic locate call and one preview verification are normally sufficient.
Do not ask the vision model to refine or verify every crop separately when the
local repeated-region fit has passed group validation.
