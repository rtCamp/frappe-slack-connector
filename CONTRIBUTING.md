## Contribution Guide

1. Install the apps with the help of the [Installation Guide](#installation).

2. Set up [pre-commit](https://pre-commit.com/) in the app.

```bash
cd frappe-slack-connector
pre-commit install
```

3. Push the code to the given branch.

```bash
cd frappe-slack-connector

git pull origin main # Make sure to pull the latest changes before making the PR

git checkout -b "new/branch"
git add --all
git commit -m ".."
git push origin "new/branch"
```

For branch names and commit messages, follow the guidelines at: https://www.conventionalcommits.org/en/v1.0.0/
