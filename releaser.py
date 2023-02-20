#!/bin/env python3
import argparse
import functools
import itertools
import pathlib
import re
import sys
import typing
from string import Template

import git


class ReleaserArgs:
    release_type: typing.Literal["major", "minor", "patch"]
    version_file: str
    repo: git.Repo
    release_branch: str
    changelog_directory: str


parser = argparse.ArgumentParser(
    prog="release",
    description="a script to release a new version of a git-based project",
)

parser.add_argument(
    "release_type",
    type=str,
    choices=["major", "minor", "patch"],
    default="patch",
    help="the type of release to make",
)
parser.add_argument(
    "-f", "--version-file", type=str, default="./VERSION", required=False
)

parser.add_argument(
    "-c", "--changelog-directory", type=str, default="./changelog", required=False
)

repo_singleton: git.Repo = None


def read_repo(arg):
    try:
        repo = git.Repo(arg)
        return repo
    except git.exc.InvalidGitRepositoryError:
        parser.error(f"Invalid git repository: {arg}")
    except:
        parser.error(f"Cannot open git repository. Unexpected error")


parser.add_argument("-r", "--repo", type=read_repo, default=".")
parser.add_argument("-b", "--release-branch", type=str, default="master")

args = parser.parse_args()  # type: ReleaserArgs


# expression from https://semver.org/
semver_re = re.compile(
    "^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: str
    build_metadata: str

    def parse(self, str):
        m = semver_re.match(str)
        if m is None:
            raise ValueError("Invalid semantic version")
        self.major = int(m.group(1))
        self.minor = int(m.group(2))
        self.patch = int(m.group(3))
        self.prerelease = m.group(4)
        self.build_metadata = m.group(5)

    def __str__(self) -> str:
        ver = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease is not None:
            ver += "-" + self.prerelease
        if self.build_metadata is not None:
            ver += "+" + self.build_metadata
        return ver

    def dump(self, fp: pathlib.Path) -> str:
        with open(fp, "w") as f:
            f.write(str(self))

    def load(self, fp: pathlib.Path):
        with open(fp, "r") as f:
            self.parse(f.read())


semantic_re = re.compile(
    "^(?P<type>feat|fix|refactor|build)(?P<scope>\(?\w*?\)?):(?P<description>.*?)$",
    re.DOTALL,
)


class ConventionalCommit:
    type: str
    scope: str
    description: str

    @classmethod
    def parse(cls, str) -> "typing.Optional[ConventionalCommit]":
        m = semantic_re.match(str)
        if m is None:
            return None
        c = cls()
        c.type = m.group(1)
        c.scope = m.group(2)
        c.description = m.group(3)
        if c.description is not None:
            c.description = c.description.strip(" \n").replace("\n", " ")
        return c

    def __str__(self) -> str:
        cc = f"{self.type}"
        if self.scope is not None and len(self.scope) > 0:
            cc += "(" + self.scope + ")"
        cc += ":"
        cc += " " + self.description
        return cc


def pre_check():
    repo = args.repo
    if repo.head.ref.name != args.release_branch:
        print(
            f"You are not on the release branch. release branch is {args.release_branch}, current branch is {repo.head.ref.name}"
        )
        sys.exit(1)

    if len(repo.untracked_files) > 0:
        print("You have untracked files. Please commit or stash them.")
        for i in repo.untracked_files:
            print(i)
        sys.exit(1)


def find_tag_by_name(repo: git.Repo, name: str) -> typing.Optional[git.TagReference]:
    for r in repo.tags:
        if r.name == name or r.name == "v" + name:
            return r
    return None


def last_tag(repo: git.Repo) -> git.TagReference:
    return repo.tags.pop()


def iter_commits_to(repo: git.Repo, sha: str):
    for commit in repo.iter_commits():
        if commit.hexsha != sha:
            yield commit
        else:
            break


RawChangelog = typing.Iterable[typing.Tuple[str, typing.Iterable[ConventionalCommit]]]


def change_log(cur_ver: SemanticVersion) -> RawChangelog:
    r = find_tag_by_name(args.repo, str(cur_ver))

    if r is None:
        r = last_tag(args.repo)

    init_sha = r.commit.hexsha if r is not None else None

    ccs = []
    for i in iter_commits_to(args.repo, init_sha):
        cc = ConventionalCommit.parse(i.message)
        if cc is None:
            continue
        ccs.append(cc)

    ccs = sorted(ccs, key=lambda x: x.type)
    m = itertools.groupby(ccs, lambda x: x.type)
    return m


def render_basic(ver: SemanticVersion, m: RawChangelog) -> str:
    out = f"# 版本更新记录 {ver} \n\n"
    for (t, d) in m:
        out += f"## {t} \n"
        out += "\n"
        out += "\n".join([f"-. {i.description}" for i in d])
        out += "\n\n"
    return out


def release():
    pre_check()
    cur_ver = SemanticVersion()
    cur_ver.load(pathlib.Path(args.version_file))
    raw = change_log(cur_ver)
    match args.release_type:
        case "major":
            cur_ver.major += 1
            cur_ver.minor = 0
            cur_ver.patch = 0
        case "minor":
            cur_ver.minor += 1
            cur_ver.patch = 0
        case "patch":
            cur_ver.patch += 1

    log = render_basic(cur_ver, raw)
    changelog_path = pathlib.Path(args.changelog_directory)
    changelog_path.mkdir(parents=True, exist_ok=True)
    with open(changelog_path / f"{cur_ver}.md", "w") as f:
        f.write(log)
    return cur_ver


def commit_change(ver: SemanticVersion):
    args.repo.git.add("--all")
    args.repo.git.commit(
        "--allow-empty",
        "--all",
        m=f"chore: release {ver}",
        untracked_files="all",
    )
    args.repo.create_tag(str(ver), message=f"release {ver}")


ver = release()
s = input("Please check the changelog. Are you sure to release (y/n)? \n>")
if s == "y" or s == "Y":
    ver.dump(args.version_file)
    commit_change(ver)
else:
    print("Aborting release...")
