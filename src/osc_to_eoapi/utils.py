import requests
from rich.console import Console
from typing import Optional

console = Console()

def test_endpoint(url: str) -> bool:
    """Tests if the STAC API endpoint is reachable."""
    try:
        resp = requests.get(f"{url}/")
        if resp.status_code == 200:
            console.print(f"[green][+] STAC API is ONLINE at {url}[/green]")
            return True
        else:
            console.print(f"[red][!] STAC API returned status {resp.status_code} at {url}[/red]")
            return False
    except Exception as e:
        console.print(f"[red][!] Failed to connect to STAC API at {url}: {e}[/red]")
        return False

def reset_database(url: str):
    """Deletes all collections from the STAC API."""
    console.print(f"[bold yellow][!] Resetting database at {url}...[/bold yellow]")
    try:
        response = requests.get(f"{url}/collections?limit=1000")
        response.raise_for_status()
        collections = response.json().get("collections", [])
        
        if not collections:
            console.print("[green][+] Database is already empty.[/green]")
            return

        for col in collections:
            col_id = col["id"]
            console.print(f"  -> Deleting collection: {col_id}")
            requests.delete(f"{url}/collections/{col_id}")

        console.print("[green][+] Database reset complete.[/green]")
    except Exception as e:
        console.print(f"[bold red][!] Database reset failed: {e}[/bold red]")
