/** Tailwind config for the NeutralBasis Control Center (build-time only). */
module.exports = {
  content: ["../static/index.html", "../static/app.js"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
