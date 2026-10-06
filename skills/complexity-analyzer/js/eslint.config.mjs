import sonarjs from "eslint-plugin-sonarjs";

export default [
  {
    files: ["**/*.js", "**/*.mjs", "**/*.cjs"],
    languageOptions: { ecmaVersion: 2022, sourceType: "script" },
    plugins: { sonarjs },
    rules: {
      // Sonar cognitive complexity: the closest analogue to
      // clippy::cognitive_complexity. 15 is Sonar's own default.
      "sonarjs/cognitive-complexity": ["warn", 15],
      // Built-in cyclomatic complexity, reported alongside as a second signal.
      "complexity": ["warn", 10]
    }
  }
];
