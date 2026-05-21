import os
import time
import json
import requests
import asyncio
import aiohttp
import pystac
from datetime import datetime, timezone
from dateutil.parser import parse
from typing import Dict, List, Optional, Set, Any
from rich.console import Console
from rich.progress import Progress

console = Console()

class OSCCrawler:
    def __init__(
        self,
        github_url: str,
        eoapi_url: str,
        update: bool = False,
        overwrite: bool = False,
        crawl_external: bool = False,
        kb_cache_file: Optional[str] = None,
        debug: bool = False,
    ):
        self.github_url = github_url
        self.base_url = github_url.rsplit('/', 1)[0]
        self.eoapi_url = eoapi_url.rstrip('/')
        self.update = update
        self.overwrite = overwrite
        self.crawl_external = crawl_external
        self.kb_cache_file = kb_cache_file
        self.debug = debug
        self.session = requests.Session()
        
        self.knowledge_base = {"projects": {}, "themes": {}, "eo-missions": {}, "variables": {}}
        self.taxonomy_folders = {
            "projects": "projects",
            "themes": "themes",
            "eo-missions": "eo-missions",
            "variables": "variables"
        }
        
        self.visited_urls: Set[str] = set()
        self.stats = {
            "collections_processed": 0,
            "items_processed": 0,
            "api_success": 0,
            "api_errors": 0,
            "fetch_errors": 0
        }
        
        self.global_bbox = [-180.0, -90.0, 180.0, 90.0]
        self.global_geometry = {
            "type": "Polygon",
            "coordinates": [[[-180.0, -90.0], [180.0, -90.0], [180.0, 90.0], [-180.0, 90.0], [-180.0, -90.0]]]
        }

    def enrich_properties(self, properties: Dict[str, Any], links: List[Any]) -> Dict[str, Any]:
        if "keywords" not in properties or not isinstance(properties["keywords"], list):
            properties["keywords"] = []
            
        for link in links:
            href = link.get("href", "") if isinstance(link, dict) else link.href
            if not href: continue
            
            for folder_name, tax_key in self.taxonomy_folders.items():
                if f"/{folder_name}/" in href:
                    try:
                        tax_id = href.split(f"/{folder_name}/")[1].split('/')[0]
                        if tax_id in self.knowledge_base[tax_key]:
                            singular = tax_key[:-1]
                            properties[f"osc:{singular}"] = tax_id
                            properties["keywords"].append(f"{singular}_{tax_id}")
                            
                            kb_data = self.knowledge_base[tax_key][tax_id]
                            properties[f"kb:{singular}:title"] = kb_data.get("title", "")
                            for k, v in kb_data.items():
                                if k.startswith('osc:'):
                                    properties[f"kb:{singular}:{k.replace('osc:', '')}"] = v
                    except IndexError:
                        pass
                        
        properties["keywords"] = list(set(properties["keywords"]))
        if not properties["keywords"]:
            properties.pop("keywords", None)
            
        return properties

    def prepare_collection(self, catalog_or_collection: pystac.STACObject, kb_links: List[Any]) -> Dict[str, Any]:
        if isinstance(catalog_or_collection, pystac.Collection):
            pgstac_collection = catalog_or_collection.clone()
        else:
            spatial_extent = pystac.SpatialExtent([self.global_bbox])
            temporal_extent = pystac.TemporalExtent([[datetime(1970, 1, 1, tzinfo=timezone.utc), None]])
            extent = pystac.Extent(spatial=spatial_extent, temporal=temporal_extent)
            pgstac_collection = pystac.Collection(
                id=catalog_or_collection.id,
                description=catalog_or_collection.description or f"Auto-generated for {catalog_or_collection.id}",
                extent=extent,
                license="proprietary"
            )
        
        if not hasattr(pgstac_collection, 'extra_fields'):
            pgstac_collection.extra_fields = {}
            
        pgstac_collection.extra_fields = self.enrich_properties(pgstac_collection.extra_fields, kb_links)
        
        enriched_keywords = pgstac_collection.extra_fields.pop("keywords", [])
        existing_keywords = pgstac_collection.keywords or []
        if enriched_keywords or existing_keywords:
            pgstac_collection.keywords = list(set(existing_keywords + enriched_keywords))
        
        col_dict = pgstac_collection.to_dict()
        col_dict["links"] = [
            {
                "rel": "queryables",
                "type": "application/schema+json",
                "title": "Queryables",
                "href": f"{self.eoapi_url}/collections/{pgstac_collection.id}/queryables"
            }
        ]
        return col_dict

    def ingest_entity(self, endpoint: str, payload: Dict[str, Any]):
        # Remove local links to avoid issues
        payload["links"] = [l for l in payload.get("links", []) if l.get("rel") in ["self", "queryables"]]
        
        try:
            target_url = f"{self.eoapi_url}/{endpoint}"
            if self.overwrite:
                # Try to delete first
                item_id = payload.get("id")
                if "items" in endpoint:
                    # e.g. collections/col_id/items/item_id
                    self.session.delete(f"{target_url}/{item_id}")
                else:
                    # e.g. collections/col_id
                    self.session.delete(target_url)

            resp = self.session.post(target_url, json=payload)
            
            if resp.status_code in (200, 201):
                self.stats["api_success"] += 1
            elif resp.status_code == 409:
                if self.update:
                    # Update existing
                    if "items" in endpoint:
                        item_id = payload.get("id")
                        put_url = f"{target_url}/{item_id}"
                        resp = self.session.put(put_url, json=payload)
                    else:
                        resp = self.session.put(target_url, json=payload)
                    
                    if resp.status_code in (200, 204):
                        self.stats["api_success"] += 1
                    else:
                        self.stats["api_errors"] += 1
                        if self.debug: console.print(f"[bold red]    [!] Failed to update {payload.get('id')}: {resp.text}[/bold red]")
                else:
                    # Skip
                    self.stats["api_success"] += 1
            else:
                self.stats["api_errors"] += 1
                if self.debug: console.print(f"[bold red]    [!] Failed to ingest {payload.get('id')}: {resp.text}[/bold red]")
        except Exception as e:
            self.stats["api_errors"] += 1
            if self.debug: console.print(f"[bold red]    [!] API connection error for {payload.get('id')}: {e}[/bold red]")

    def build_knowledge_base(self, root_catalog: pystac.Catalog):
        if self.debug:
            console.print(f"[dim]Debug: kb_cache_file is set to: '{self.kb_cache_file}'[/dim]")
        if self.kb_cache_file and os.path.exists(self.kb_cache_file):
            console.print(f"[bold blue]Loading Knowledge Base from cache file: {self.kb_cache_file}...[/bold blue]")
            try:
                with open(self.kb_cache_file, "r") as f:
                    self.knowledge_base = json.load(f)
                for k, v in self.knowledge_base.items():
                    console.print(f"[*] Loaded {len(v)} {k} from cache.")
                return
            except Exception as e:
                console.print(f"[bold red]Failed to load cache: {e}. Rebuilding...[/bold red]")

        console.print("[bold blue]Building Knowledge Base from taxonomies...[/bold blue]")
        for link in root_catalog.get_child_links():
            href = link.get_absolute_href()
            if not href or not href.startswith(self.base_url): continue
                
            for folder_name, tax_key in self.taxonomy_folders.items():
                if f"/{folder_name}/" in href:
                    console.print(f"  -> Crawling {folder_name} (as {tax_key})...")
                    tax_catalog = link.resolve_stac_object().target
                    
                    for entity_link in tax_catalog.get_child_links():
                        try:
                            entity = entity_link.resolve_stac_object().target
                            kb_data = {"title": entity.title}
                            if hasattr(entity, 'extra_fields'):
                                for k, v in entity.extra_fields.items():
                                    if k.startswith('osc:'):
                                        kb_data[k] = v
                            self.knowledge_base[tax_key][entity.id] = kb_data
                        except Exception as e:
                            console.print(f"[yellow]      [!] Failed to read KB entity {entity_link.href}: {e}[/yellow]")
                            
        for k, v in self.knowledge_base.items():
            console.print(f"[*] Loaded {len(v)} {k} into memory.")
            
        if self.kb_cache_file:
            try:
                with open(self.kb_cache_file, "w") as f:
                    json.dump(self.knowledge_base, f, indent=2)
                console.print(f"[bold green]Saved Knowledge Base to cache file: {self.kb_cache_file}[/bold green]")
            except Exception as e:
                console.print(f"[yellow]Failed to save KB cache to {self.kb_cache_file}: {e}[/yellow]")

    async def _async_ingest_entity(self, session: aiohttp.ClientSession, endpoint: str, payload: Dict[str, Any]):
        payload["links"] = [l for l in payload.get("links", []) if l.get("rel") in ["self", "queryables"]]
        target_url = f"{self.eoapi_url}/{endpoint}"
        item_id = payload.get("id")

        try:
            if self.overwrite:
                del_url = f"{target_url}/{item_id}" if "items" in endpoint else target_url
                async with session.delete(del_url) as resp:
                    await resp.read()

            async with session.post(target_url, json=payload) as resp:
                status = resp.status
                text = await resp.text()

            if status in (200, 201):
                self.stats["api_success"] += 1
            elif status == 409:
                if self.update:
                    put_url = f"{target_url}/{item_id}" if "items" in endpoint else target_url
                    async with session.put(put_url, json=payload) as resp:
                        if resp.status in (200, 204):
                            self.stats["api_success"] += 1
                        else:
                            self.stats["api_errors"] += 1
                            if self.debug: console.print(f"[bold red]    [!] Failed to update {item_id}: {await resp.text()}[/bold red]")
                else:
                    self.stats["api_success"] += 1
            else:
                self.stats["api_errors"] += 1
                if self.debug: console.print(f"[bold red]    [!] Failed to ingest {item_id}: {text}[/bold red]")
        except Exception as e:
            self.stats["api_errors"] += 1
            if self.debug: console.print(f"[bold red]    [!] API connection error for {item_id}: {e}[/bold red]")

    async def _async_process_record(self, session: aiohttp.ClientSession, href: str, parent_links: List[Any], collection_id: str) -> Optional[Dict[str, Any]]:
        if self.debug: console.print(f"      [dim]Fetching item: {href}[/dim]")
        try:
            async with session.get(href) as resp:
                if resp.status != 200:
                    self.stats["fetch_errors"] += 1
                    if self.debug: console.print(f"[red]    [!] HTTP {resp.status} fetching record {href}[/red]")
                    return None
                record_data = await resp.json()
        except Exception as e:
            self.stats["fetch_errors"] += 1
            if self.debug: console.print(f"[red]    [!] Failed to fetch record {href}: {e}[/red]")
            return None
            
        record_id = record_data.get("id", f"{href.split('/')[-2]}-record")
        geom = record_data.get("geometry", self.global_geometry) or self.global_geometry
        bbox = record_data.get("bbox", self.global_bbox) or self.global_bbox
        
        properties = record_data.get("properties", {})
        if not isinstance(properties, dict): properties = {}
            
        for k, v in record_data.items():
            if k not in ["id", "geometry", "bbox", "type", "properties", "links", "assets", "stac_version", "stac_extensions"]:
                properties[f"record:{k}"] = v
                
        all_links = record_data.get("links", []) + parent_links
        properties = self.enrich_properties(properties, all_links)
            
        dt_obj = datetime.now(timezone.utc)
        if "datetime" in properties and properties["datetime"]:
            try:
                dt_obj = parse(properties["datetime"])
                if dt_obj.tzinfo is None: dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            except: pass
                
        item = pystac.Item(id=record_id, geometry=geom, bbox=bbox, datetime=dt_obj, properties=properties)
        item.collection_id = collection_id
        return item.to_dict()

    async def _async_process_and_ingest(self, session: aiohttp.ClientSession, sem: asyncio.Semaphore, href: str, links: List[Any], collection_id: str) -> int:
        async with sem:
            item_dict = await self._async_process_record(session, href, links, collection_id)
            if item_dict:
                await self._async_ingest_entity(session, f"collections/{collection_id}/items", item_dict)
                return 1
            return 0

    async def _async_find_and_ingest_items(self, session: aiohttp.ClientSession, sem: asyncio.Semaphore, catalog_link: pystac.Link, collection_links: List[Any], collection_id: str, visited: Set[str], depth: int = 0) -> int:
        href = catalog_link.get_absolute_href()
        if not href or href in visited:
            if self.debug and href in visited:
                console.print(f"{'  ' * depth}      [dim][Skip] Already visited catalog: {href}[/dim]")
            return 0
        visited.add(href)
        
        if self.debug:
            console.print(f"{'  ' * depth}      [dim]Exploring catalog: {href}[/dim]")
            
        items_processed = 0
        tasks = []
        
        try:
            # We resolve the catalog structure synchronously using pystac's native methods,
            # but queue the individual item downloads to happen concurrently.
            catalog = catalog_link.resolve_stac_object().target
            if not isinstance(catalog, (pystac.Catalog, pystac.Collection)):
                return 0
                
            # Queue up all items in this catalog for concurrent processing
            for item_link in catalog.get_item_links():
                item_href = item_link.get_absolute_href()
                if item_href:
                    task = asyncio.create_task(self._async_process_and_ingest(session, sem, item_href, catalog.links + collection_links, collection_id))
                    tasks.append(task)
            
            # Recurse into children synchronously, but gather their task counts
            for sub_link in catalog.get_child_links():
                items_processed += await self._async_find_and_ingest_items(session, sem, sub_link, collection_links, collection_id, visited, depth + 1)
                
            # Await all item ingestions for this catalog level concurrently
            if tasks:
                results = await asyncio.gather(*tasks)
                items_processed += sum(results)
                
        except Exception as e:
            if self.debug: console.print(f"{'  ' * depth}      [yellow][!] Failed exploring catalog {href}: {e}[/yellow]")
            
        return items_processed

    async def _async_run_collection_ingestion(self, collection: pystac.Collection, entity_link: pystac.Link, category: Optional[str]) -> int:
        connector = aiohttp.TCPConnector(limit=50)
        sem = asyncio.Semaphore(20) # Limit concurrent item processing
        
        async with aiohttp.ClientSession(connector=connector) as session:
            items_ingested = 0
            tasks = []
            
            if category in ["experiments", "workflows"]:
                record_href = entity_link.get_absolute_href().replace("collection.json", "record.json").replace("catalog.json", "record.json")
                tasks.append(asyncio.create_task(self._async_process_and_ingest(session, sem, record_href, collection.links, collection.id)))
            else:
                # 1. Directly linked items
                for item_link in collection.get_item_links():
                    href = item_link.get_absolute_href()
                    if href:
                        tasks.append(asyncio.create_task(self._async_process_and_ingest(session, sem, href, collection.links, collection.id)))
                
                # 2. Child links (external item catalogs)
                for child_link in collection.get_child_links():
                    child_href = child_link.get_absolute_href()
                    if not child_href: continue
                    if self.crawl_external or child_link.title == "Items":
                        if self.debug: console.print(f"      [dim]Starting external item search in: {child_href}[/dim]")
                        items_ingested += await self._async_find_and_ingest_items(session, sem, child_link, collection.links, collection.id, visited=set(), depth=1)
            
            if tasks:
                results = await asyncio.gather(*tasks)
                items_ingested += sum(results)
                
            return items_ingested

    def _ingest_collection_and_items(self, collection: pystac.Collection, entity_link: pystac.Link, category: Optional[str] = None):
        start_time = time.time()
        col_dict = self.prepare_collection(collection, collection.links)
        
        # Ingest the collection synchronously first
        self.ingest_entity("collections", col_dict)
        self.stats["collections_processed"] += 1
        console.print(f"  [+] Ingested Collection: [bold]{collection.id}[/bold]")
        
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        items_ingested = loop.run_until_complete(self._async_run_collection_ingestion(collection, entity_link, category))
        
        self.stats["items_processed"] += items_ingested
        
        elapsed_time = time.time() - start_time
        console.print(f"      └── Fetched and ingested [bold cyan]{items_ingested}[/bold cyan] items in [yellow]{elapsed_time:.2f}s[/yellow]")

    def crawl_catalog(self, catalog: pystac.Catalog, is_root: bool = False, is_external: bool = False):
        href = catalog.get_self_href()
        if href in self.visited_urls:
            return
        self.visited_urls.add(href)
        
        target_categories = ["products", "experiments", "workflows"]
        
        for link in catalog.get_child_links():
            child_href = link.get_absolute_href()
            if not child_href: continue
            
            # Check if it's an OSC internal link or an external link
            is_internal = child_href.startswith(self.base_url)
            
            if not is_internal and not self.crawl_external:
                continue

            try:
                child = link.resolve_stac_object().target
            except Exception as e:
                console.print(f"[yellow]  [!] Failed to resolve child {child_href}: {e}[/yellow]")
                continue

            if is_internal and not is_external:
                category = next((c for c in target_categories if f"/{c}/" in child_href), None)
                if not category and not is_root:
                    if isinstance(child, pystac.Catalog):
                        self.crawl_catalog(child)
                    continue

                if category:
                    console.print(f"\n[bold green]Processing Data Category: {category}...[/bold green]")
                    if isinstance(child, pystac.Catalog):
                        for entity_link in child.get_child_links():
                            try:
                                collection = entity_link.resolve_stac_object().target
                                if isinstance(collection, pystac.Collection):
                                    self._ingest_collection_and_items(collection, entity_link, category)
                            except Exception as e:
                                self.stats["fetch_errors"] += 1
                                console.print(f"  [red]  [!] Failed processing collection {entity_link.href}: {e}[/red]")
            else:
                # External crawl logic
                if isinstance(child, pystac.Collection):
                    console.print(f"\n[bold magenta]Processing External Collection: {child.id}...[/bold magenta]")
                    try:
                        self._ingest_collection_and_items(child, link, category=None)
                    except Exception as e:
                        self.stats["fetch_errors"] += 1
                        console.print(f"  [red]  [!] Failed processing external collection {child_href}: {e}[/red]")
                elif isinstance(child, pystac.Catalog):
                    console.print(f"[blue]  -> Crawling external catalog: {child_href}[/blue]")
                    self.crawl_catalog(child, is_external=True)

    def run(self):
        console.print(f"[bold cyan]Starting Crawl from: {self.github_url}[/bold cyan]")
        try:
            root_catalog = pystac.Catalog.from_file(self.github_url)
        except Exception as e:
            console.print(f"[bold red]Failed to load root catalog: {e}[/bold red]")
            return

        self.build_knowledge_base(root_catalog)
        self.crawl_catalog(root_catalog, is_root=True)
        
        console.print("\n" + "=" * 50)
        console.print(" 🏁 [bold]CRAWL EXECUTION SUMMARY[/bold]")
        console.print("=" * 50)
        console.print(f"Collections Processed: {self.stats['collections_processed']}")
        console.print(f"Items Processed:       {self.stats['items_processed']}")
        console.print(f"Successful API Posts:  {self.stats['api_success']}")
        console.print(f"API Errors (Failed):   {self.stats['api_errors']}")
        console.print(f"Fetch/Read Errors:     {self.stats['fetch_errors']}")
        console.print("=" * 50)
