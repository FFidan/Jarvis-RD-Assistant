// Tailwind config snippet — merge into your existing tailwind.config.ts
// Extends colors with v5 tokens, adds Source Serif 4 + JetBrains Mono families.
//
// Usage: copy the contents of `theme.extend` and `fontFamily` into your config.

import type { Config } from "tailwindcss";

const v5Extend: Partial<Config["theme"]> = {
  extend: {
    colors: {
      ink: {
        DEFAULT: "#0b3a8a",
        soft:   "rgba(11, 58, 138, 0.08)",
        border: "rgba(11, 58, 138, 0.15)",
      },
      paper: "#fbfaf7",
      cream: "#fdf9f0",
      cool:  "#f5f8fe",
      project: {
        blue:   "#2563eb",
        green:  "#16a34a",
        purple: "#9333ea",
        pink:   "#db2777",
        amber:  "#d97706",
      },
    },
    fontFamily: {
      sans:  ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      serif: ['"Source Serif 4"', '"Source Serif Pro"', 'ui-serif', 'Georgia', 'serif'],
      mono:  ['"JetBrains Mono"', 'ui-monospace', '"SF Mono"', 'Menlo', 'monospace'],
    },
    maxWidth: {
      page: "860px",
    },
    boxShadow: {
      hero: "0 1px 0 rgba(0, 0, 0, 0.02)",
    },
  },
};

export default v5Extend;
