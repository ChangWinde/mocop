# ADR-0009: Orthogonal visual style and accent

## Status

Accepted

## Context

The original browser preference stored five named themes. Most choices changed color, while two also changed material and geometry. This mixed two independent decisions and made each new color require another full theme.

## Driving factors

- Offer meaningfully different layouts without duplicating component markup.
- Let an operator choose a complete curated color palette independently from visual density and material.
- Preserve existing browser preferences without server storage or a migration endpoint.
- Do not accept custom CSS or remote theme assets.

## Candidates

### Option A: Keep adding complete named themes

Pros: one preference and no migration.

Cons: style and color combinations grow multiplicatively, CSS drifts, and most choices remain recolors.

### Option B: Separate curated style families from curated accents

Pros: six styles and six accents form 36 reviewed combinations, share one component tree, and remain bounded browser-local values.

Cons: selectors and keyboard controls must manage two independent radio groups.

### Option C: Accept user-authored CSS themes

Pros: unlimited customization.

Cons: weakens CSP, creates an unsafe rendering surface, and makes layout compatibility untestable.

## Decision

Choose Option B. Persist one validated `visualStyle` and one validated `accent`. Style families own layout, spacing, typography, geometry, and material; accents select a complete bounded palette for emphasis, ambient, surface, line, and interaction colors. Migrate the five legacy `theme` values to the nearest curated pair when the new fields are absent.

## Impact

- The dashboard provides six structurally distinct styles and six independent accents without new runtime dependencies.
- Existing browser preferences migrate on read and are rewritten in the new form after the next preference change.
- Appearance remains local to one browser and never enters HTTP, collection configuration, or remote commands.
- Adding a style requires responsive and browser coverage; adding an accent requires a bounded palette-difference and contrast review.

> **Update:** the read-time migration of the five pre-0.9 `theme` values was
> removed in 0.9.0; a preference record without `visualStyle`/`accent` now
> simply receives the defaults.
