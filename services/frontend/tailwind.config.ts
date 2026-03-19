import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // ── Loadshare brand palette ──────────────────────────────
        ls: {
          blue:    "#4280FF",   // primary brand — CTAs, accents, links, highlights
          white:   "#FFFFFF",   // backgrounds, content areas
          dark:    "#222222",   // primary text, headings
          mid:     "#555555",   // secondary text, subheaders
          surface: "#F5F7FA",   // panel backgrounds, cards
          border:  "#E5E7EB",   // dividers, borders
          // ── Dashboard dark canvas (ops monitoring) ────────────
          canvas:  "#0F1117",   // dashboard root background
          panel:   "#1A1F2E",   // panel / card backgrounds on dark canvas
          panelHover: "#1F2640", // hovered panel bg
        },
        // Zone stress level colors (ops-specific, brand-adjacent)
        zone: {
          dead:     "#EF4444",  // red
          low:      "#F97316",  // orange
          normal:   "#22C55E",  // green
          stressed: "#EAB308",  // yellow
          stale:    "#6B7280",  // grey
        },
        // Severity badges
        severity: {
          critical: "#EF4444",
          warning:  "#F97316",
          normal:   "#22C55E",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      animation: {
        "pulse-blue": "pulse-blue 2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "fade-in":    "fade-in 0.3s ease-out",
      },
      keyframes: {
        "pulse-blue": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(66, 128, 255, 0.4)" },
          "50%":      { boxShadow: "0 0 0 8px rgba(66, 128, 255, 0)" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
