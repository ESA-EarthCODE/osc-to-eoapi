import json
import subprocess
import typer
from typing import Optional, List
from osc_to_eoapi.crawler import OSCCrawler
from osc_to_eoapi.utils import test_endpoint, reset_database

app = typer.Typer(help="OSC to eoapi STAC Crawler CLI")

@app.command()
def crawl(
    github_url: str = typer.Option(
        "https://raw.githubusercontent.com/ESA-EarthCODE/open-science-catalog-metadata/main/catalog.json",
        "--github-url",
        help="URL to the root OSC STAC catalog.json on GitHub"
    ),
    eoapi_url: str = typer.Option(
        "http://localhost:8080",
        "--eoapi-url",
        help="URL to the eoapi STAC API instance"
    ),
    update: bool = typer.Option(False, "--update", help="Update existing collections/items"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing collections/items by deleting first"),
    reset_db: bool = typer.Option(False, "--reset-db", help="Clear all collections before starting"),
    test_api: bool = typer.Option(False, "--test-endpoint", help="Test if the STAC API is reachable before starting"),
    crawl_external: bool = typer.Option(False, "--crawl-external", help="Enable recursive crawling of external STAC links"),
    kb_cache: str = typer.Option("kb_cache.json", "--kb-cache", help="Path to cache the knowledge base. Set to empty string '' to disable."),
    skip_collection: Optional[List[str]] = typer.Option(None, "--skip-collection", help="Collection ID to skip if already present in the API"),
    categories: Optional[List[str]] = typer.Option(["products", "experiments", "workflows"], "--category", help="Categories to crawl. Can be specified multiple times (e.g. --category workflows --category experiments)"),
    debug: bool = typer.Option(False, "--debug", help="Enable verbose debug logging for catalog traversal"),
):
    """
    Crawls an OSC STAC catalog and ingests it into an eoapi instance.
    """
    if test_api:
        if not test_endpoint(eoapi_url):
            raise typer.Exit(code=1)
            
    if reset_db:
        reset_database(eoapi_url)
        
    # Convert empty string to None to disable caching
    actual_kb_cache = kb_cache if kb_cache.strip() else None
        
    crawler = OSCCrawler(
        github_url=github_url,
        eoapi_url=eoapi_url,
        update=update,
        overwrite=overwrite,
        crawl_external=crawl_external,
        kb_cache_file=actual_kb_cache,
        skip_collections=skip_collection,
        categories=categories,
        debug=debug
    )
    crawler.run()

@app.command()
def load_queryables(
    schema_path: Optional[str] = typer.Option(None, "--schema", help="Path to custom queryables JSON schema"),
):
    """
    Loads custom queryables schema into PgSTAC.
    """
    if not schema_path:
        # Default OSC queryables schema
        queryables_schema = {
            "$schema": "https://json-schema.org/draft/2019-09/schema",
            "$id": "https://eoapi.workspace.publishing-support.earthcode.eox.at/stac/queryables",
            "type": "object",
            "title": "ESA OSC Flattened Queryables",
            "properties": {
                "osc:project": {"description": "The ID of the associated project", "type": "string"},
                "osc:theme": {"description": "The ID of the associated theme", "type": "string"},
                "osc:eo-mission": {"description": "The ID of the associated mission", "type": "string"},
                "osc:variable": {"description": "The ID of the associated variable", "type": "string"},
                "kb:project:title": {"description": "The human-readable title of the project", "type": "string"},
                "kb:theme:title": {"description": "The human-readable title of the theme", "type": "string"},
                "kb:eo-mission:title": {"description": "The human-readable title of the mission", "type": "string"},
                "kb:variable:title": {"description": "The human-readable title of the variable", "type": "string"}
            }
        }
        schema_path = "osc_queryables.json"
        with open(schema_path, "w") as f:
            json.dump(queryables_schema, f, indent=2)
            
    typer.echo("Calling pypgstac CLI to load flattened queryables...")
    result = subprocess.run(
        ["pypgstac", "load_queryables", schema_path],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        typer.secho("[+] Successfully loaded queryables into PgSTAC!", fg=typer.colors.GREEN)
        if result.stdout.strip():
            typer.echo(f"    Output: {result.stdout.strip()}")
    else:
        typer.secho("[!] Error loading queryables:", fg=typer.colors.RED)
        typer.echo(result.stderr)

if __name__ == "__main__":
    app()
