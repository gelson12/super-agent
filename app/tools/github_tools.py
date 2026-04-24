import base64
import requests
from github import Github, GithubException
from langchain_core.tools import tool
from ..config import settings
from ..cache.tool_cache import cached_tool


def _client() -> Github:
    return Github(settings.github_pat)


@tool
def github_list_repos(filter: str = "") -> str:
    """List all GitHub repositories for gelson12. Pass a filter string to search by name."""
    try:
        user = _client().get_user("gelson12")
        repos = [r.name for r in user.get_repos() if filter.lower() in r.name.lower()]
        return "\n".join(repos) if repos else "No repositories found."
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_list_files(repo_name: str, path: str = "", branch: str = "main") -> str:
    """List files and folders in a GitHub repo at a given path. repo_name is just the name e.g. 'super-agent'."""
    try:
        repo = _client().get_repo(f"gelson12/{repo_name}")
        # Try main then master if branch not found
        for ref in [branch, "master", "main"]:
            try:
                contents = repo.get_contents(path, ref=ref)
                items = [f"{'[DIR]' if c.type == 'dir' else '[FILE]'} {c.path}" for c in contents]
                return "\n".join(items) if items else "Empty directory."
            except GithubException as e:
                if e.status == 404 and ref != branch:
                    continue
                raise
        return "[GitHub error: branch not found]"
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
@cached_tool(ttl=300)
def github_read_file(repo_name: str, file_path: str, branch: str = "main") -> str:
    """Read the content of a file from a GitHub repository."""
    try:
        repo = _client().get_repo(f"gelson12/{repo_name}")
        for ref in [branch, "master", "main"]:
            try:
                f = repo.get_contents(file_path, ref=ref)
                return base64.b64decode(f.content).decode("utf-8")
            except GithubException as e:
                if e.status == 404 and ref != branch:
                    continue
                raise
        return "[GitHub error: file not found]"
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_create_or_update_file(
    repo_name: str,
    file_path: str,
    content: str,
    commit_message: str,
    branch: str = "main",
) -> str:
    """Create or update a file in a GitHub repository. content is the full file text."""
    try:
        repo = _client().get_repo(f"gelson12/{repo_name}")
        encoded = content.encode("utf-8")
        try:
            existing = repo.get_contents(file_path, ref=branch)
            repo.update_file(file_path, commit_message, encoded, existing.sha, branch=branch)
            return f"Updated {file_path} in {repo_name} on branch '{branch}'."
        except GithubException as e:
            if e.status == 404:
                repo.create_file(file_path, commit_message, encoded, branch=branch)
                return f"Created {file_path} in {repo_name} on branch '{branch}'."
            raise
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_delete_file(
    repo_name: str,
    file_path: str,
    commit_message: str,
    branch: str = "main",
) -> str:
    """Delete a file from a GitHub repository."""
    try:
        repo = _client().get_repo(f"gelson12/{repo_name}")
        f = repo.get_contents(file_path, ref=branch)
        repo.delete_file(file_path, commit_message, f.sha, branch=branch)
        return f"Deleted {file_path} from {repo_name} on branch '{branch}'."
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_create_branch(
    repo_name: str,
    branch_name: str,
    from_branch: str = "main",
) -> str:
    """Create a new branch in a GitHub repository."""
    try:
        repo = _client().get_repo(f"gelson12/{repo_name}")
        # Try from_branch, then master
        for ref in [from_branch, "master", "main"]:
            try:
                source = repo.get_branch(ref)
                repo.create_git_ref(f"refs/heads/{branch_name}", source.commit.sha)
                return f"Created branch '{branch_name}' from '{ref}' in {repo_name}."
            except GithubException as e:
                if e.status == 404 and ref != from_branch:
                    continue
                raise
        return "[GitHub error: source branch not found]"
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_create_pull_request(
    repo_name: str,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str = "main",
) -> str:
    """Create a pull request in a GitHub repository."""
    try:
        repo = _client().get_repo(f"gelson12/{repo_name}")
        pr = repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
        return f"Pull request created: {pr.html_url}"
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_create_repo(
    repo_name: str,
    description: str = "",
    private: bool = False,
    auto_init: bool = True,
) -> str:
    """Create a new GitHub repository under gelson12. auto_init=True seeds main with a README so Pages can be enabled immediately."""
    try:
        user = _client().get_user()
        repo = user.create_repo(
            name=repo_name,
            description=description,
            private=private,
            auto_init=auto_init,
        )
        return f"Created repo {repo.full_name} (default_branch={repo.default_branch}, html_url={repo.html_url})."
    except GithubException as e:
        return f"[GitHub error: {e}]"


@tool
def github_enable_pages(
    repo_name: str,
    branch: str = "main",
    path: str = "/",
) -> str:
    """Enable GitHub Pages on a repo. Source = the given branch/path. URL becomes https://gelson12.github.io/{repo_name}/."""
    url = f"https://api.github.com/repos/gelson12/{repo_name}/pages"
    headers = {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {"source": {"branch": branch, "path": path}}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        if r.status_code in (201, 204):
            return f"Pages enabled on gelson12/{repo_name} ({branch}{path}). URL: https://gelson12.github.io/{repo_name}/"
        if r.status_code == 409:
            return f"Pages already enabled on gelson12/{repo_name}. URL: https://gelson12.github.io/{repo_name}/"
        return f"[GitHub error: {r.status_code} {r.text}]"
    except requests.RequestException as e:
        return f"[GitHub error: {e}]"
