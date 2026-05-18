# Typography Contract

This document is the canonical source for headline rhythm and caption usage
across the JARVIS RD Assistant frontend. It exists because
[`docs/archive/2026-05/2026-05-07-ui-headline-rhythm-audit.md`](archive/2026-05/2026-05-07-ui-headline-rhythm-audit.md)
catalogued 28 HIGH findings where the `SectionHeader` marker, `<h2>`/`<h3>`
siblings, `CardTitle`, and tab labels were all competing to caption the same
visual block. Fixing those instances was Wave-1 of the
[fix-and-feature sweep](archive/2026-05/2026-05-07-tier-1-2-3-fix-and-feature-sweep.md);
this contract is what keeps them from coming back.

There is no ESLint enforcement: the frontend's `npm run lint` is
`tsc --noEmit` only, and adding ESLint is out of scope. The hand-review
checklist below is the substitute.

## The 4-level contract

```
PAGE LEVEL (max 1 per route)
  H1 — text-[28-32px] leading-tight tracking-tight text-strong
  Subtitle — text-sm text-muted-foreground (one line, one paragraph)

NAVIGATION LEVEL (tabs, surface chips)
  Tab labels — owned by TabsTrigger / role=tab. Treat them as captions.
  Rule — ban any heading inside TabsContent whose text equals or is a
  word-stem subset of the active tab label.

SECTION LEVEL (group of >=2 sibling sub-blocks, no other caption available)
  SectionHeader / MarkerCaption marker — the existing § small-caps span,
  used ONLY when:
    a) the section contains >=2 sibling sub-blocks each with their own
       CardTitle / heading, AND
    b) no parent (page H1, tab label, Card containing this section) has
       already named the same concept.
  Forbidden uses — directly above a single Card, directly above a single
  Cytoscape canvas, directly above a Tabs strip, inside a TabsContent,
  inside a Card whose CardTitle would repeat it.

CARD LEVEL (owns its visual border)
  CardTitle — required if the card is more than a thin row of inputs.
  CardDescription — optional, single short paragraph.
  Rule — no SectionHeader above; no <h2>/<h3> directly below CardHeader
  before the first <CardContent> child.

INLINE LEVEL (form labels, field captions, micro-block titles)
  Label component (already exists) — for form inputs.
  MarkerLabel — `components/typography/MarkerLabel.tsx`. Replaces ad-hoc
  small-caps `<h3>` usage in AppearanceSection / AutomationSection.

DIALOG / SHEET LEVEL
  DialogTitle / SheetTitle — required, owns the modal caption.
  Rule — body of a Dialog/Sheet must not open with another heading whose
  text equals the DialogTitle.
```

## One caption per visual block

Each visual block (Card, TabsContent, Sheet, Section) is allowed at most
one caption that names the block itself. Sub-blocks inside it can each
have their own caption, but the block-level caption must not be repeated
by an immediate parent or child. This is the simplest mental model that
captures every finding from the audit: when you find yourself writing two
captions for the same thing, delete one.

## Hand-review checklist

When reviewing a PR that touches frontend headings, run this list against
the diff. Anything that fails is a request-changes signal.

- [ ] No `<SectionHeader>` (or `<MarkerCaption>`) is rendered directly
  inside a `<TabsContent>` whose marker text matches the active tab label.
- [ ] No `<SectionHeader>` (or `<MarkerCaption>`) appears immediately
  above a `<Card>` whose `<CardTitle>` shares the same word stem.
- [ ] No `<h2>` or `<h3>` is a sibling of a `<CardHeader>` repeating the
  card label.
- [ ] At most one caption per visual block (Card / TabsContent / Sheet /
  Section).
- [ ] Page H1 is set exactly once per route, in the page component.
- [ ] Sidebar item label, browser tab title, and page H1 use the same
  canonical name.

## When the contract changes

This document is the canonical source. Update it here first, then update
[`docs/ENGINEERING_STANDARDS.md`](ENGINEERING_STANDARDS.md) Typography
contract subsection if the change is material. Do not duplicate the rules
into individual page files or component JSDoc; link back here.

## References

- [UI headline rhythm audit (2026-05-07)](archive/2026-05/2026-05-07-ui-headline-rhythm-audit.md)
  — the 28 HIGH findings that motivated this contract.
- [Tier 1+2+3 fix-and-feature sweep (2026-05-07)](archive/2026-05/2026-05-07-tier-1-2-3-fix-and-feature-sweep.md)
  — Wave-1 removed every existing offender; Wave-2 (this) ratifies the
  contract.
- `frontend/src/components/typography/MarkerCaption.tsx` — the section
  marker component.
- `frontend/src/components/typography/MarkerLabel.tsx` — the inline
  small-caps label primitive.
