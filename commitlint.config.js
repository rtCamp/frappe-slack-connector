module.exports = {
  parserPreset: "conventional-changelog-conventionalcommits",
  // Imported from version-16; its shared history cannot be rewritten.
  ignores: [
    (message) =>
      message.trim() === "Run Copilot code review on GitHub-hosted runners",
  ],
  rules: {
    "subject-empty": [2, "never"],
    "type-case": [2, "always", "lower-case"],
    "type-empty": [2, "never"],
    "type-enum": [
      2,
      "always",
      [
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
      ],
    ],
  },
};
