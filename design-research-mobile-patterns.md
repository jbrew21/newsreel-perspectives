# Mobile Design Patterns Research: Perspectives App
## Dark-Mode News/Media App — "Today's Fractures" + Voice Profiles
### Research Date: 2026-03-13

---

## 1. DARK MODE COLOR SYSTEM (Production-Ready Values)

### Background Layers (from darkest to lightest)
Use a **3-layer depth system** — NOT pure black (#000), which is too harsh except on OLED.

| Layer | Purpose | Recommended Hex | Notes |
|-------|---------|----------------|-------|
| **Base** | App background | `#0A0A0B` | Near-black, slight warmth. iOS uses #000 but warmer reads better for content apps |
| **Surface 1** | Cards, containers | `#141416` | 1st elevation. This is your story card bg |
| **Surface 2** | Nested elements, hover states | `#1C1C1F` | Chips, argument cluster containers |
| **Surface 3** | Active/pressed states | `#252528` | Selected states, modal backgrounds |
| **Elevated** | Tooltips, popovers | `#2C2C30` | Top-level overlays |

### iOS System Dark Colors (for reference)
- Primary background: `#000000`
- Secondary background: `#1C1C1E`
- Tertiary background: `#2C2C2E`
- Primary text: `#FFFFFF`
- Secondary text: `rgba(235, 235, 245, 0.6)` — `#EBEBF5` at 60%
- Tertiary text: `rgba(235, 235, 245, 0.3)` — `#EBEBF5` at 30%
- Separator: `rgba(84, 84, 88, 0.65)` — `#545458` at 65%
- Thin separator: `rgba(84, 84, 88, 0.35)`

### Tailwind Zinc Scale (excellent neutral dark mode palette)
```
zinc-950: oklch(14.1% 0.005 285.823)  ≈ #18181B  — card backgrounds
zinc-900: oklch(21% 0.006 285.885)    ≈ #27272A  — elevated surfaces
zinc-800: oklch(27.4% 0.006 286.033)  ≈ #3F3F46  — borders, dividers
zinc-700: oklch(37% 0.013 285.805)    ≈ #52525B  — muted text, icons
zinc-400: oklch(70.5% 0.015 286.067)  ≈ #A1A1AA  — secondary text
zinc-200: oklch(92% 0.004 286.32)     ≈ #E4E4E7  — primary text
zinc-50:  oklch(98.5% 0 0)            ≈ #FAFAFA  — headlines, emphasis
```

### Accent Colors for Fracture Lines / Argument Clusters
Don't use saturated colors at full brightness on dark — **desaturate by 10-20% and reduce lightness**.

| Purpose | Color | Hex | Usage |
|---------|-------|-----|-------|
| **Cluster A** (e.g., progressive) | Soft blue | `#60A5FA` (tw blue-400) | Cluster borders, dot indicators |
| **Cluster B** (e.g., conservative) | Soft red/coral | `#F87171` (tw red-400) | Cluster borders, dot indicators |
| **Cluster C** (e.g., libertarian/independent) | Soft amber | `#FBBF24` (tw amber-400) | Cluster borders, dot indicators |
| **Cluster D** (e.g., centrist/mixed) | Soft purple | `#A78BFA` (tw violet-400) | Cluster borders, dot indicators |
| **Fracture line** | Gradient | `linear-gradient(135deg, #60A5FA, #F87171)` | The visual "crack" between clusters |
| **High tension** | Pulsing glow | `#EF4444` at 20% opacity | For stories with sharp disagreement |

---

## 2. "TODAY'S FRACTURES" HOMEPAGE — Story Cards

### Card Container Design

**Best patterns observed across Twitter/X, Apple News, Robinhood, Artifact:**

```
Card Container:
  background: #141416 (Surface 1)
  border-radius: 16px (industry standard for modern iOS cards)
  border: 1px solid rgba(255, 255, 255, 0.06)  — ultra-subtle edge
  padding: 16px
  margin-bottom: 12px (gap between cards)

  /* NO drop shadows in dark mode — they disappear. Use borders + subtle glow instead */
  box-shadow: none

  /* Optional: subtle inner glow for depth */
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03)
```

### Story Card Layout (Mobile, 375px viewport)

```
┌──────────────────────────────────────┐
│  CATEGORY CHIP          TENSION BAR  │ ← 12px from top
│  Iran Nuclear Deal       ████░░ 73%  │
│                                      │
│  Story Headline That               │ ← 20-22px, font-weight 700
│  Wraps to Two Lines                │    letter-spacing: -0.02em
│                                      │
│  One-line summary in secondary text  │ ← 14px, zinc-400, 1 line
│                                      │
│  ┌─ CLUSTER A ──────┐┌─ CLUSTER B ─┐│ ← Argument clusters
│  │ "Should negotiate"││"Must isolate"│
│  │ 😀😀😀😀         ││ 😀😀😀      ││ ← Stacked avatars
│  │ +3 more           ││ +2 more     ││
│  └───────────────────┘└─────────────┘│
│                                      │
│  ·····fracture line gradient······  │ ← Visual separator
│                                      │
│  12 voices · 3 clusters · 2h ago    │ ← 12px, zinc-600
└──────────────────────────────────────┘
```

### Typography Hierarchy

| Element | Size | Weight | Color | Line-height | Letter-spacing |
|---------|------|--------|-------|-------------|----------------|
| **Story headline** | 20px (1.25rem) | 700 (Bold) | `#FAFAFA` (zinc-50) | 1.25 (25px) | -0.02em |
| **Summary line** | 14px (0.875rem) | 400 (Regular) | `#A1A1AA` (zinc-400) | 1.4 (19.6px) | 0 |
| **Category chip** | 11px (0.6875rem) | 600 (Semibold) | Accent color | 1 | 0.05em (uppercase) |
| **Cluster label** | 13px (0.8125rem) | 600 (Semibold) | `#E4E4E7` (zinc-200) | 1.3 | 0 |
| **Cluster argument** | 13px | 400 | `#A1A1AA` (zinc-400) | 1.4 | 0 |
| **Metadata line** | 12px (0.75rem) | 400 | `#52525B` (zinc-700) | 1.3 | 0 |
| **Tension %** | 14px | 700 | Accent (red if high, amber if mid) | 1 | 0 |

**Font recommendation:** SF Pro Display (iOS native) or Inter (web). Both have excellent dark-mode legibility.

### Category Chip Design
```css
.category-chip {
  display: inline-flex;
  padding: 4px 10px;
  border-radius: 100px;       /* pill shape */
  background: rgba(96, 165, 250, 0.12);  /* accent at 12% opacity */
  color: #60A5FA;              /* accent color */
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
```

### Tension/Fracture Bar
```css
.tension-bar {
  width: 48px;
  height: 4px;
  border-radius: 2px;
  background: #27272A;        /* zinc-900 track */
}
.tension-bar-fill {
  height: 100%;
  border-radius: 2px;
  /* Gradient from calm to heated */
  background: linear-gradient(90deg, #FBBF24, #EF4444);
}
```

---

## 3. AVATAR / VOICE GROUPING PATTERNS

### Stacked Avatar Group (Best Practice from GitHub, Atlassian, Linear)

```css
.avatar-group {
  display: flex;
  flex-direction: row-reverse;  /* stack right-to-left */
}

.avatar-group .avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 2px solid #141416;   /* matches card background — creates "cut" effect */
  margin-left: -8px;           /* overlap: 8px (roughly 28% of diameter) */
  object-fit: cover;
  position: relative;
}

.avatar-group .avatar:last-child {
  margin-left: 0;
}

.avatar-overflow {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #27272A;
  border: 2px solid #141416;
  margin-left: -8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  color: #A1A1AA;
}
```

### Size Variants (Common across design systems)

| Size name | Diameter | Border | Overlap | Use case |
|-----------|----------|--------|---------|----------|
| **xs** | 20px | 1.5px | -6px | Compact lists, metadata |
| **sm** | 28px | 2px | -8px | Story cards, cluster groups |
| **md** | 36px | 2px | -10px | Featured sections |
| **lg** | 44px | 2.5px | -12px | Voice profile headers |
| **xl** | 56px | 3px | -14px | Hero sections |

### Cluster Avatar Layout (Your Use Case)
For argument clusters showing 3-5 voices per position:

```
┌── Cluster: "Should negotiate" ─────────┐
│                                         │
│  (😀)(😀)(😀)(😀) +3                   │  ← 28px avatars, -8px overlap
│                                         │
│  "Diplomacy is the only path..."       │  ← 13px, italic, zinc-400
│                                         │
│  AOC · Bernie · Warren · Kaine          │  ← 11px, zinc-600
└─────────────────────────────────────────┘

Container:
  background: rgba(96, 165, 250, 0.06)   /* cluster accent at 6% */
  border-left: 2px solid #60A5FA         /* accent left border = cluster identity */
  border-radius: 12px
  padding: 12px
```

---

## 4. FRACTURE LINE VISUAL DESIGN

The "fracture" between argument clusters is your signature design element. Options:

### Option A: Gradient Divider with Glow
```css
.fracture-line {
  height: 1px;
  margin: 12px 0;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(96, 165, 250, 0.5),    /* cluster A color */
    rgba(248, 113, 113, 0.5),   /* cluster B color */
    transparent
  );
  box-shadow: 0 0 8px rgba(96, 165, 250, 0.15),
              0 0 8px rgba(248, 113, 113, 0.15);
}
```

### Option B: Jagged/Torn Line (SVG)
Use an SVG pattern that looks like a crack or tear:
```css
.fracture-line-jagged {
  height: 8px;
  background-image: url("data:image/svg+xml,..."); /* zigzag SVG */
  opacity: 0.3;
  filter: drop-shadow(0 0 4px rgba(239, 68, 68, 0.2));
}
```

### Option C: Animated Pulse (High Tension Stories)
```css
.fracture-line-pulse {
  height: 2px;
  background: linear-gradient(90deg, #60A5FA, #EF4444);
  animation: fracture-pulse 2s ease-in-out infinite;
}
@keyframes fracture-pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; box-shadow: 0 0 12px rgba(239, 68, 68, 0.3); }
}
```

---

## 5. VOICE PROFILE PAGE

### Layout Pattern (Inspired by Twitter/X, Spotify, Robinhood)

```
┌─────────────────────────────────────────┐
│ ← Back              ···  (more menu)    │ ← 44px nav bar
│                                         │
│         ┌──────┐                        │
│         │ PHOTO│   56px avatar          │
│         └──────┘                        │
│                                         │
│     Tucker Carlson                      │ ← 24px, weight 700
│     @TuckerCarlson · Fox News           │ ← 14px, zinc-400
│                                         │
│     "Former Fox News host,              │ ← 14px, zinc-400, 2-line max
│      conservative commentator"          │
│                                         │
│  ┌─────────┐ ┌──────────┐ ┌─────────┐  │
│  │42 Topics│ │28 Allies │ │15 Clashes│  │ ← Stat pills
│  └─────────┘ └──────────┘ └─────────┘  │
│                                         │
│─────────────────────────────────────────│ ← Tab bar
│  Positions    Alliances    Timeline     │
│  ─────────                              │ ← Active underline
│                                         │
│ ┌─ Iran Nuclear Deal ──────────────────┐│
│ │ Position: "Iran cannot be trusted"   ││ ← Position card
│ │ Cluster: Hawkish                     ││
│ │ Agrees with: ●●● Disagrees: ●●●    ││ ← Mini avatar dots
│ │ 3 posts · Last: 2d ago              ││
│ └──────────────────────────────────────┘│
│                                         │
│ ┌─ Ukraine War ────────────────────────┐│
│ │ Position: "End the war, negotiate"   ││
│ │ Cluster: Anti-intervention           ││
│ │ Agrees with: ●●● Disagrees: ●●●●   ││
│ └──────────────────────────────────────┘│
└─────────────────────────────────────────┘
```

### Profile Header Specs
```css
.profile-header {
  padding: 24px 20px 16px;
  text-align: center;
}

.profile-avatar {
  width: 72px;
  height: 72px;
  border-radius: 50%;
  border: 3px solid #27272A;
  margin-bottom: 12px;
}

.profile-name {
  font-size: 22px;
  font-weight: 700;
  color: #FAFAFA;
  line-height: 1.2;
  letter-spacing: -0.01em;
}

.profile-handle {
  font-size: 14px;
  color: #71717A;  /* zinc-500 */
  margin-top: 2px;
}

.profile-bio {
  font-size: 14px;
  color: #A1A1AA;
  margin-top: 8px;
  line-height: 1.4;
  max-lines: 2;
}
```

### Stat Pills
```css
.stat-pill {
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  padding: 8px 16px;
  background: #1C1C1F;
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  gap: 2px;
}

.stat-pill-number {
  font-size: 18px;
  font-weight: 700;
  color: #FAFAFA;
}

.stat-pill-label {
  font-size: 11px;
  color: #71717A;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
```

### Position Card (On Voice Profile)
```css
.position-card {
  background: #141416;
  border-radius: 14px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  padding: 14px 16px;
  margin-bottom: 10px;
}

.position-topic {
  font-size: 11px;
  font-weight: 600;
  color: #71717A;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}

.position-quote {
  font-size: 15px;
  font-weight: 500;
  color: #E4E4E7;
  line-height: 1.4;
  font-style: italic;
  margin-bottom: 8px;
}

.position-cluster-badge {
  display: inline-flex;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  /* color depends on cluster */
  background: rgba(accent, 0.1);
  color: accent;
}
```

### Alliance Visualization (Shifting Alliances Feature)
Show who this voice agrees/disagrees with across topics:

```
┌─ Alliances ─────────────────────────────┐
│                                          │
│  Most aligned with:                      │
│  (😀)(😀)(😀) DeSantis · Vance · Cruz   │ ← green tint
│  Agree on 8/12 topics                    │
│                                          │
│  Most divergent from:                    │
│  (😀)(😀)(😀) AOC · Warren · Sanders    │ ← red tint
│  Agree on 1/12 topics                   │
│                                          │
│  Surprising agreements:                  │ ← This is the "wow" section
│  (😀) Tulsi Gabbard — agree on 6/12    │ ← amber tint
│  (😀) Joe Rogan — agree on 5/12        │
│                                          │
└──────────────────────────────────────────┘
```

```css
.alliance-row {
  display: flex;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}

.alliance-bar {
  height: 3px;
  border-radius: 1.5px;
  background: #27272A;
  flex: 1;
  margin: 0 12px;
}

.alliance-bar-fill {
  height: 100%;
  border-radius: 1.5px;
}

/* Agreement bar colors */
.alliance-bar-fill.high    { background: #4ADE80; width: 80%; }  /* green-400 */
.alliance-bar-fill.medium  { background: #FBBF24; width: 50%; }  /* amber-400 */
.alliance-bar-fill.low     { background: #F87171; width: 20%; }  /* red-400 */
```

---

## 6. INTERACTION PATTERNS ("WOW" MOMENTS)

### Card Expansion (Tap to Expand Story)
```css
.story-card {
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

.story-card.expanded {
  border-radius: 20px;
  /* Card expands to show full argument clusters, all avatars, quotes */
  transform: scale(1.02);
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08),
              0 8px 40px rgba(0, 0, 0, 0.4);
}
```

### Swipe Between Clusters (Horizontal scroll within a story card)
```css
.cluster-scroll {
  display: flex;
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  -webkit-overflow-scrolling: touch;
  gap: 8px;
  padding-bottom: 8px;
}

.cluster-scroll::-webkit-scrollbar {
  display: none;
}

.cluster-item {
  scroll-snap-align: start;
  min-width: 70%;           /* each cluster takes 70% of card width */
  flex-shrink: 0;
}
```

### Avatar Hover/Tap Reveal
On tap, show voice name + quick-position tooltip:
```css
.avatar-tooltip {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  background: #27272A;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  padding: 6px 10px;
  white-space: nowrap;
  font-size: 12px;
  color: #E4E4E7;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);

  animation: tooltip-pop 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
}

@keyframes tooltip-pop {
  from { opacity: 0; transform: translateX(-50%) scale(0.9) translateY(4px); }
  to   { opacity: 1; transform: translateX(-50%) scale(1) translateY(0); }
}
```

### Parallax Story Header (Optional, for detail view)
As user scrolls into a story detail, the headline sticks and clusters slide up underneath:
```css
.story-detail-header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: linear-gradient(180deg, #0A0A0B 80%, transparent);
  padding: 16px 20px;
}
```

### Spotify Wrapped-Style Data Storytelling
For a "Your Perspectives Recap" or onboarding flow:
- Full-screen cards, swipe vertically
- Bold sans-serif numbers (48-72px, weight 800)
- Vibrant gradients as backgrounds (not cards)
- Key insight per screen: "Tucker & AOC agreed on 3 topics this week"
- Typography: mix massive display type with tiny labels
- Example gradient: `linear-gradient(160deg, #1a1a2e, #16213e, #0f3460)`

---

## 7. INFORMATION DENSITY ON MOBILE

### Patterns from High-Density Apps (Robinhood, Bloomberg, X)

**Rule of thumb:** Max 3 levels of information per card.
1. **Level 1** — What is it? (Headline + category)
2. **Level 2** — Why should I care? (Tension %, cluster count)
3. **Level 3** — Who's involved? (Avatar clusters)

**Do NOT show all at once.** Use progressive disclosure:
- **Collapsed card:** Headline + tension bar + avatar row (compact)
- **Tapped/expanded:** Full clusters + argument quotes + voice names
- **Detail view:** All positions, quotes, timeline, sources

### Compact Card Variant (For 4+ stories on screen)
```
┌──────────────────────────────────────┐
│ Iran Nuclear Deal          73% ████░ │ ← 16px bold, tension bar
│ 12 voices · 3 clusters             │ ← 12px, zinc-600
│ (😀)(😀)(😀)(😀)(😀) +7            │ ← 24px avatars, tighter
└──────────────────────────────────────┘
  Height: ~72px | Padding: 12px
```

### Spacing System
Use a **4px base grid** (matches iOS/Material):
```
4px   — micro spacing (between icon and label)
8px   — tight spacing (between related elements)
12px  — default element spacing
16px  — card padding (all sides)
20px  — section padding (horizontal margins)
24px  — section separation
32px  — major section breaks
```

---

## 8. KEY DESIGN SYSTEM TOKENS SUMMARY

```css
:root {
  /* Backgrounds */
  --bg-base: #0A0A0B;
  --bg-surface-1: #141416;
  --bg-surface-2: #1C1C1F;
  --bg-surface-3: #252528;
  --bg-elevated: #2C2C30;

  /* Text */
  --text-primary: #FAFAFA;
  --text-secondary: #A1A1AA;
  --text-tertiary: #71717A;
  --text-muted: #52525B;

  /* Borders */
  --border-subtle: rgba(255, 255, 255, 0.06);
  --border-default: rgba(255, 255, 255, 0.1);
  --border-strong: rgba(255, 255, 255, 0.16);

  /* Cluster accents */
  --cluster-blue: #60A5FA;
  --cluster-red: #F87171;
  --cluster-amber: #FBBF24;
  --cluster-violet: #A78BFA;
  --cluster-green: #4ADE80;
  --cluster-cyan: #22D3EE;

  /* Functional */
  --tension-low: #4ADE80;
  --tension-medium: #FBBF24;
  --tension-high: #EF4444;

  /* Radii */
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --radius-xl: 20px;
  --radius-full: 9999px;

  /* Avatar */
  --avatar-xs: 20px;
  --avatar-sm: 28px;
  --avatar-md: 36px;
  --avatar-lg: 44px;
  --avatar-xl: 56px;
  --avatar-border: 2px solid var(--bg-surface-1);
  --avatar-overlap: -8px;

  /* Typography */
  --font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --text-xs: 11px;
  --text-sm: 13px;
  --text-base: 14px;
  --text-md: 15px;
  --text-lg: 18px;
  --text-xl: 20px;
  --text-2xl: 24px;
  --text-display: 32px;
}
```

---

## 9. SOURCES & REFERENCES

### Design Systems Consulted
- **Apple HIG** — iOS dark mode colors, system semantics
- **Tailwind CSS** — Zinc/Slate/Neutral scales (full values extracted)
- **Geist (Vercel)** — 10-step color scale architecture (bg/border/text layers)
- **Refactoring UI** — 9-shade palette construction, "trust your eyes" principle
- **GitHub Primer** — Dark mode token architecture
- **Carbon (IBM)** — Dark theme layer system
- **shadcn/ui** — Avatar + AvatarGroup component structure

### App Patterns Referenced
- **Twitter/X** — Profile layout, tab navigation, feed card density
- **Robinhood** — Data viz cards, stat pills, dark mode depth
- **Spotify Wrapped** — Data storytelling, bold type, gradient backgrounds
- **Apple News** — Story card hierarchy, category chips, image treatments
- **Linear** — Stacked avatars, dark mode card borders, subtle glows
- **Discord** — Dark surface layering, avatar groups, role colors
- **Artifact (RIP)** — News card layout, swipe patterns, topic badges

### Key Principles Applied
1. **No pure black backgrounds** — Use #0A-#14 range for warmth
2. **No drop shadows in dark mode** — Use 6% white borders and subtle inner glows instead
3. **Desaturate accent colors** — Use Tailwind -400 variants, never -500 or -600 on dark
4. **Border, not shadow, for elevation** — `rgba(255,255,255,0.06)` is the magic number
5. **Progressive disclosure** — Collapsed > Expanded > Detail for information density
6. **Overlap avatars by ~28% of diameter** — Creates cohesive group without losing identity
7. **4px spacing grid** — Everything divisible by 4
8. **Max 3 info levels per card** — What, Why, Who
