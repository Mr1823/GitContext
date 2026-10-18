# GitContext interface design

Adapt the dark, type-led developer-tool direction in the Nuxt reference for GitContext's repository workflow. Keep the existing GitContext wordmark, orbit mark, lime accent, backgrounds, borders, and features. Use the reference for hierarchy, readable code presentation, and deliberate spacing; do not copy its interface or tokens verbatim.

## System

```css
:root {
  --bg: #111410;
  --panel: #171b16;
  --panel-soft: #1b201a;
  --border: #2b3229;
  --muted: #899286;
  --text: #eef1e9;
  --accent: #b9f279;
  --accent-strong: #9bdc56;
  --sans: "DM Sans", sans-serif;
  --display: "Manrope", sans-serif;
  --mono: "DM Mono", monospace;
  --space-1: 6px;
  --space-2: 12px;
  --space-3: 16px;
  --space-4: 24px;
  --space-5: 32px;
  --space-6: 48px;
  --space-7: 64px;
  --type-caption: 12px;
  --type-body: 14px;
  --type-section: 16px;
  --type-display: clamp(34px, 4.2vw, 52px);
  --radius-control: 6px;
  --radius-panel: 10px;
  --radius-pill: 999px;
  --content-max: 1440px;
}
```

## Application

- Keep repository connection, source files, indexing state, cited chat, and the indexed architecture diagram as the page's work areas.
- Place the diagram above the repository and chat columns so import relationships can be read before opening individual files.
- Give the repository question a clear display heading and keep the orbit mark as its visual counterpoint.
- Use the lime accent for primary actions and focus states. Keep source paths and code excerpts in the mono face.
- Increase reading sizes for section titles, chat text, source labels, and code while preserving the existing narrow layout and mobile stacking.
- Keep the header, logo, and orbit mark recognizable at 360px, 768px, and 1280px viewport widths.

## Reference

Nuxt, captured by Inspo on 2026-05-04: dark mode, green accents, strong type hierarchy, and code-focused presentation. Adapted for GitContext; original site and tokens remain its authors' work.
