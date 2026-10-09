/** Tailwind build config. The compiled stylesheet is committed
 * (nexus/web/static/css/app.css) so the app needs no Node at runtime and
 * renders fully offline. Rebuild after changing templates: npm run build:css
 */
const verdictColors = ['emerald', 'amber', 'rose', 'slate'];

module.exports = {
  content: [
    './nexus/web/templates/**/*.html',
    './nexus/**/*.py',
  ],
  // Classes assembled at render time, e.g. border-{{ color }}-800 on the
  // file-scan verdict cards (security.html, transfer.html).
  safelist: verdictColors.flatMap((c) => [
    `border-${c}-800`, `border-${c}-800/60`,
    `bg-${c}-950/40`, `bg-${c}-950/20`,
    `text-${c}-200`, `text-${c}-300`,
  ]),
  theme: {
    extend: {
      colors: { brand: { DEFAULT: '#fbbf24', soft: '#fcd34d', deep: '#b45309' } },
      fontFamily: {
        sans: ['system-ui', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(251,191,36,.15), 0 8px 30px -12px rgba(251,191,36,.25)',
      },
      keyframes: {
        fadeIn: { '0%': { opacity: 0, transform: 'translateY(4px)' }, '100%': { opacity: 1, transform: 'none' } },
      },
      animation: { fadeIn: 'fadeIn .25s ease-out both' },
    },
  },
};
