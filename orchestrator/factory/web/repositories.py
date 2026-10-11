"""Read-only GitHub discovery for the Console. Credentials never leave the server."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from ..config import Config
from ..forge.github import GitHubForge


def repository_name(url: str, cfg: Config) -> str:
    parsed, host = urlparse(url), urlparse(cfg.forge_url)
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if (parsed.scheme != 'https' or parsed.netloc.lower() != host.netloc.lower()
            or parsed.query or parsed.fragment or len(parts) != 2
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part in ('.', '..') for part in parts)):
        raise HTTPException(400, 'Choose a repository on the configured GitHub host, using its HTTPS repository URL.')
    return '/'.join(parts)


def list_repositories(cfg: Config, page: int) -> dict:
    if cfg.forge != 'github':
        raise HTTPException(409, 'Repository browsing is available for GitHub. Enter the repository URL for this code host.')
    if not cfg.forge_token:
        raise HTTPException(409, 'Connect GitHub with FORGE_TOKEN in Settings to browse repositories.')
    forge = GitHubForge(cfg.forge_url, cfg.forge_token, '', timeout=15)
    try:
        response = forge.http.get(f'{forge.api}/user/repos', params={
            'affiliation': 'owner,collaborator,organization_member', 'sort': 'updated',
            'direction': 'desc', 'per_page': 100, 'page': page,
        })
        if response.status_code == 401:
            raise HTTPException(401, 'GitHub did not accept the configured token. Check the connection in Settings.')
        if response.status_code in (403, 429):
            raise HTTPException(503, 'GitHub access is restricted or rate limited. Check repository access or try again later.')
        response.raise_for_status()
        repositories = []
        for item in response.json():
            reason = ('Archived' if item.get('archived') else 'Disabled' if item.get('disabled') else
                      'Read-only access' if not (item.get('permissions') or {}).get('push') else None)
            repositories.append({
                'full_name': item['full_name'], 'url': item['html_url'],
                'default_branch': item.get('default_branch') or 'main',
                'private': bool(item.get('private')), 'selectable': reason is None, 'unavailable_reason': reason,
                # a new repository can be created under the connection's own account or an organisation, never
                # under another person's account it merely collaborates on (the add-app form reads these)
                'owner_type': (item.get('owner') or {}).get('type'),
                'admin': bool((item.get('permissions') or {}).get('admin')),
            })
        return {'repositories': repositories, 'next_page': page + 1 if 'next' in response.links else None}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, 'GitHub repositories could not be loaded. Try again.') from exc
    finally:
        forge.http.close()
