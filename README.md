# git-learning

Project to learn about and practise git commands.

## Setup new repository

**Prerequisites**

SSH key generated and added to GitHub settings

```bash
ssh-keygen -t ed25519 -C "dwstanton@yahoo.com"
```

No password needed.

**Setup instructions**

First create a repository in GitHub.

1. Login into GitHub account.
2. Create new repository
3. Clone repository:

```bash
git clone git@github.com:SturdyDave/git-learning.git
```

## Getting started with git 

```bash
# create a new branch to work in (without affecting the main branch)
git checkout -b name-of-branch

# creates new branch from current branch and then checks out new branch
```

```bash
# displays latest changes
git status
```

```bash
# commit your changes to the local git history
git commit -a -m "made this change" 
```

```bash
# push your changes up to the remote repository
git push
```