from __future__ import annotations
import os
from pathlib import Path
from playwright.sync_api import BrowserContext

def _candidate_chrome_user_data_dirs() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    home = Path.home()

    candidates: list[Path] = []
    if local:
        candidates.append(Path(local) / "Google" / "Chrome" / "User Data")
    candidates.append(home / "AppData" / "Local" / "Google" / "Chrome" / "User Data")

    seen: set[str] = set()
    unique: list[Path] = []
    for p in candidates:
        rp = str(p.resolve())
        if rp not in seen and p.exists():
            seen.add(rp)
            unique.append(p)
    return unique


def _profile_candidates(user_data_dir: Path) -> list[str]:
    names: list[str] = []
    if not user_data_dir.exists():
        return ["Default"]

    for p in user_data_dir.iterdir():
        if not p.is_dir():
            continue
        n = p.name
        if n == "Default" or n.startswith("Profile "):
            names.append(n)

    def sort_key(name: str):
        if name == "Default":
            return (0, 0)
        try:
            return (1, int(name.split("Profile ", 1)[1]))
        except Exception:
            return (2, name)

    names.sort(key=sort_key, reverse=True)
    return names or ["Default"]


def _launch(
    p,
    *,
    chrome_exe: str,
    user_data_dir: Path,
    profile_name: str,
) -> BrowserContext:
    return p.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        executable_path=chrome_exe,
        headless=False,
        args=[
            f"--profile-directory={profile_name}",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=document-user-activation-required",
            "--mute-audio",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )


def launch_social_context(p) -> BrowserContext:
    chrome_exe = os.getenv("CHROME_EXECUTABLE_PATH", "").strip()
    env_user_data = os.getenv("CHROME_USER_DATA_DIR", "").strip()
    env_profile = os.getenv("CHROME_PROFILE_DIR", "").strip()

    if chrome_exe and env_user_data and env_profile:
        user_data_dir = Path(env_user_data).expanduser().resolve()
        profile_name = env_profile
        print(f"Using .env Chrome profile: {user_data_dir} :: {profile_name}")
        return _launch(
            p,
            chrome_exe=chrome_exe,
            user_data_dir=user_data_dir,
            profile_name=profile_name,
        )

    candidate_user_data_dirs = _candidate_chrome_user_data_dirs()
    last_error = None

    for user_data_dir in candidate_user_data_dirs:
        for profile_name in _profile_candidates(user_data_dir):
            try:
                print(f"Trying Chrome profile: {user_data_dir} :: {profile_name}")
                return _launch(
                    p,
                    chrome_exe=chrome_exe,
                    user_data_dir=user_data_dir,
                    profile_name=profile_name,
                )
            except Exception as exc:
                last_error = exc
                print(f"Chrome profile failed: {profile_name} -> {exc}")

    fallback_dir = Path(
        os.getenv("PLAYWRIGHT_SOCIAL_PROFILE_DIR", "./.playwright-social-profile")
    ).resolve()
    fallback_dir.mkdir(parents=True, exist_ok=True)

    if not chrome_exe:
        raise RuntimeError(
            "CHROME_EXECUTABLE_PATH is required because Chrome is not in Playwright's default location."
        ) from last_error

    print(f"Falling back to Playwright profile: {fallback_dir}")
    return _launch(
        p,
        chrome_exe=chrome_exe,
        user_data_dir=fallback_dir,
        profile_name="Default",
    )