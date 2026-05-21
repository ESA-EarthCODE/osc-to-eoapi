# osc-to-eoapi

A Python package to crawl OSC (Open Science Catalog) STAC catalogs and ingest them into an `eoapi` STAC API instance.

## Installation

```bash
pip install .
```

## Usage

### Ingesting Data

```bash
osc-to-eoapi crawl [OPTIONS]
```

**Options:**

- `--github-url TEXT`: URL to the root OSC STAC `catalog.json` on GitHub. (Default: ESA OSC main branch)
- `--eoapi-url TEXT`: URL to the `eoapi` STAC API instance. (Default: `http://localhost:8080`)
- `--update`: If a collection/item already exists (409), attempt to update it with a `PUT` request.
- `--overwrite`: Force overwrite by deleting existing collections/items before ingestion.
- `--reset-db`: Clear **all** collections from the target STAC API before starting the crawl.
- `--test-endpoint`: Perform a health check on the STAC API before starting.
- `--crawl-external`: Enable recursive crawling of external STAC links found in the catalog. Includes cycle detection to prevent infinite loops.
- `--kb-cache TEXT`: Path to a local JSON file to cache the taxonomies (variables, projects, etc.) to significantly speed up subsequent runs. In order to disable it set it to an empty string. (Default: `kb_cache.json`)
- `--debug`: Enable verbose debug logging to trace the recursive traversal of external catalogs and item discovery. Useful for identifying bottlenecks or infinite loops in remote datasets.

### Loading Queryables

To enable filtering by the custom OSC properties (e.g., `osc:project`, `kb:variable:title`), load the queryables schema:

```bash
osc-to-eoapi load-queryables
```

You can also provide a custom schema:
```bash
osc-to-eoapi load-queryables --schema ./my-schema.json
```

## Publishing to PyPI

1. Ensure you have `build` and `twine` installed:
   ```bash
   pip install build twine
   ```
2. Build the package:
   ```bash
   python -m build
   ```
3. Upload to PyPI:
   ```bash
   python -m twine upload dist/*
   ```

## Development

1. Create and activate a Python virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Linux/macOS
   # OR: venv\Scripts\activate # On Windows
   ```

2. Install in editable mode with dependencies:
   ```bash
   pip install -e .
   ```

### Local Testing Environment

A `docker-compose.yml` is provided to easily spin up a local PgSTAC database and `eoapi` STAC API instance for testing.

1. Start the local infrastructure:
   ```bash
   docker compose up -d
   ```

2. Export local database credentials (required for `pypgstac` to load queryables). A `.env` file is provided, which you can source directly:
   ```bash
   set -a; source .env; set +a
   ```

3. Load the custom queryables into the local database:
   ```bash
   osc-to-eoapi load-queryables
   ```

4. Run the crawler against the local API (running on `http://localhost:8080`):
   ```bash
   osc-to-eoapi crawl --test-endpoint --reset-db --eoapi-url http://localhost:8080
   ```
   *(Add `--crawl-external` if you want to test recursive external link crawling).*

5. Tear down the local infrastructure and wipe test data when finished:
   ```bash
   docker compose down -v
   ```
