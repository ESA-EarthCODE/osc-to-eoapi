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
        skip_collections: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        add_source_links: bool = False,
        source_base_url: Optional[str] = None,
        debug: bool = False,
    ):
        self.github_url = github_url
        self.base_url = github_url.rsplit('/', 1)[0]
        self.eoapi_url = eoapi_url.rstrip('/')
        self.update = update
        self.overwrite = overwrite
        self.crawl_external = crawl_external
        self.kb_cache_file = kb_cache_file
        self.skip_collections = set(skip_collections or [])
        self.categories = categories or ["products", "experiments", "workflows"]
        self.add_source_links = add_source_links
        self.source_base_url = source_base_url.rstrip('/') if source_base_url else None
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
            "products_processed": 0,
            "product_items_processed": 0,
            "workflows_processed": 0,
            "experiments_processed": 0,
            "collections_skipped": 0,
            "api_success": 0,
            "api_errors": 0,
            "fetch_errors": 0
        }
        
        self.global_bbox = [-180.0, -90.0, 180.0, 90.0]
        self.global_geometry = {
            "type": "Polygon",
            "coordinates": [[[-180.0, -90.0], [180.0, -90.0], [180.0, 90.0], [-180.0, 90.0], [-180.0, -90.0]]]
        }

    def _get_source_url(self, url: str) -> str:
        """Translates a source URL to use the source_base_url if configured."""
        if not self.source_base_url or not url.startswith(self.base_url):
            return url
        return url.replace(self.base_url, self.source_base_url, 1)

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
                            # Only "project" is a valid osc:* singular property here according to the schema
                            if singular == "project":
                                properties["osc:project"] = tax_id
                            else:
                                properties[f"kb:{singular}"] = tax_id
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

    def prepare_collection(self, catalog_or_collection: pystac.STACObject, kb_links: List[Any], source_url: Optional[str] = None) -> Dict[str, Any]:
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
        
        # Manually inject extra_fields as PySTAC clone/to_dict can drop them for Collections
        for k, v in getattr(pgstac_collection, 'extra_fields', {}).items():
            if k not in col_dict:
                col_dict[k] = v

        # Fix invalid BBOX [90, 180, -90, -180] if present
        if "extent" in col_dict and "spatial" in col_dict["extent"] and "bbox" in col_dict["extent"]["spatial"]:
            bboxes = col_dict["extent"]["spatial"]["bbox"]
            for i, bbox in enumerate(bboxes):
                if bbox == [90, 180, -90, -180]:
                    console.print(f"[bold yellow]  [!] Warning: Detected invalid global BBOX [90, 180, -90, -180] in {pgstac_collection.id}. Correcting to [-180, -90, 180, 90].[/bold yellow]")
                    col_dict["extent"]["spatial"]["bbox"][i] = [-180, -90, 180, 90]
        
        self_href = f"{self.eoapi_url}/collections/{pgstac_collection.id}"
        if self.add_source_links and source_url:
            self_href = self._get_source_url(source_url)
                
        col_dict["links"] = [
            {
                "rel": "self",
                "type": "application/json",
                "href": self_href
            },
            {
                "rel": "queryables",
                "type": "application/schema+json",
                "title": "Queryables",
                "href": f"{self.eoapi_url}/collections/{pgstac_collection.id}/queryables"
            }
        ]
        
        if self.add_source_links and source_url:
            source_href = self._get_source_url(source_url)
            col_dict["links"].append({"rel": "canonical", "type": "application/json", "href": source_href})
            
        return col_dict

    def prepare_links(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        links = payload.get("links", [])
        allowed_rels = ["self", "queryables", "related", "child", "parent", "item", "about", "derived_from", "root", "canonical"]
        fixed_links = []
        
        for link in links:
            rel = link.get("rel")
            if rel not in allowed_rels:
                continue
                
            href = link.get("href", "")
            
            # If add_source_links is enabled, translate absolute self, parent, root, canonical links too
            if self.add_source_links and rel in ["self", "parent", "root", "canonical"] and href.startswith("http"):
                link["href"] = self._get_source_url(href)
                fixed_links.append(link)
                continue

            if not href or href.startswith("http"):
                fixed_links.append(link)
                continue
                
            # Rewrite relative OSC links to STAC API links
            parts = href.split('/')
            new_href = None
            
            # Only attempt API mapping if not preferring source links
            if not self.add_source_links:
                if "workflows" in parts:
                    idx = parts.index("workflows")
                    if len(parts) > idx + 1:
                        wf_id = parts[idx + 1]
                        if "workflow" not in wf_id.lower():
                            wf_id = f"{wf_id}-workflow"
                        new_href = f"{self.eoapi_url}/collections/{wf_id}"
                elif "products" in parts:
                    idx = parts.index("products")
                    if len(parts) > idx + 1:
                        prod_id = parts[idx + 1]
                        new_href = f"{self.eoapi_url}/collections/{prod_id}"
            
            if new_href:
                link["href"] = new_href
                # For STAC API links, we prefer application/json
                link["type"] = "application/json"
            else:
                # Fallback: make it an absolute link to the source GitHub repository
                # This ensures the link isn't broken even if we can't map it to the API
                clean_href = href.lstrip('./').replace('../', '', 2)
                abs_href = f"{self.base_url}/{clean_href}"
                link["href"] = self._get_source_url(abs_href)
            
            fixed_links.append(link)
            
        return fixed_links

    def ingest_entity(self, endpoint: str, payload: Dict[str, Any]) -> bool:
        # Update and filter links
        payload["links"] = self.prepare_links(payload)
        entity_id = payload.get("id")
        
        try:
            target_url = f"{self.eoapi_url}/{endpoint}"
            individual_url = target_url
            if "items" not in endpoint:
                individual_url = f"{target_url}/{entity_id}"
            else:
                # endpoint is likely collections/col_id/items
                individual_url = f"{target_url}/{entity_id}"

            if self.overwrite:
                # Try to delete first
                self.session.delete(individual_url)

            resp = self.session.post(target_url, json=payload)
            
            if resp.status_code in (200, 201):
                self.stats["api_success"] += 1
                return True
            elif resp.status_code == 409:
                if self.update:
                    # Update existing
                    resp = self.session.put(individual_url, json=payload)

                    if resp.status_code in (200, 204):
                        self.stats["api_success"] += 1
                        return True
                    else:
                        self.stats["api_errors"] += 1
                        console.print(f"[bold red]    [!] Failed to update {entity_id}: {resp.text}[/bold red]")
                        return False
                else:
                    # Skip (consider success as it exists)
                    self.stats["api_success"] += 1
                    return True

            else:
                self.stats["api_errors"] += 1
                console.print(f"[bold red]    [!] Failed to ingest {entity_id} at {target_url}: {resp.text}[/bold red]")
                return False
        except Exception as e:
            self.stats["api_errors"] += 1
            console.print(f"[bold red]    [!] API connection error for {entity_id}: {e}[/bold red]")
            return False

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
        payload["links"] = self.prepare_links(payload)
        target_url = f"{self.eoapi_url}/{endpoint}"
        entity_id = payload.get("id")
        individual_url = target_url
        if "items" not in endpoint:
            individual_url = f"{target_url}/{entity_id}"
        else:
            individual_url = f"{target_url}/{entity_id}"

        try:
            if self.overwrite:
                async with session.delete(individual_url) as resp:
                    await resp.read()

            async with session.post(target_url, json=payload) as resp:
                status = resp.status
                text = await resp.text()

            if status in (200, 201):
                self.stats["api_success"] += 1
            elif status == 409:
               if self.update:
                   async with session.put(individual_url, json=payload) as resp:
                       if resp.status in (200, 204):
                           self.stats["api_success"] += 1
                       else:
                           self.stats["api_errors"] += 1
                           console.print(f"[bold red]    [!] Failed to update {entity_id}: {await resp.text()}[/bold red]")
               else:
                   self.stats["api_success"] += 1
            else:
                self.stats["api_errors"] += 1
                console.print(f"[bold red]    [!] Failed to ingest {entity_id} at {target_url}: {text}[/bold red]")
        except Exception as e:
            self.stats["api_errors"] += 1
            console.print(f"[bold red]    [!] API connection error for {entity_id}: {e}[/bold red]")

    async def _async_process_record(self, session: aiohttp.ClientSession, href: str, parent_links: List[Any], collection_id: Optional[str] = None, category: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if self.debug: console.print(f"      [dim]Fetching item: {href}[/dim]")
        try:
            async with session.get(href) as resp:
                if resp.status != 200:
                    self.stats["fetch_errors"] += 1
                    if self.debug: console.print(f"[red]    [!] HTTP {resp.status} fetching record {href}[/red]")
                    return None
                record_data = await resp.json(content_type=None)
        except Exception as e:
            self.stats["fetch_errors"] += 1
            if self.debug: console.print(f"[red]    [!] Failed to fetch record {href}: {e}[/red]")
            return None
            
        record_id = record_data.get("id", f"{href.split('/')[-2]}-record")
        if category == "experiments" and "experiment" not in record_id.lower():
            record_id = f"{record_id}-experiment"
            
        geom = record_data.get("geometry", self.global_geometry) or self.global_geometry
        bbox = record_data.get("bbox", self.global_bbox) or self.global_bbox
        
        properties = record_data.get("properties", {})
        if not isinstance(properties, dict): properties = {}
            
        for k, v in record_data.items():
            if k not in ["id", "geometry", "bbox", "type", "properties", "links", "assets", "stac_version", "stac_extensions"]:
                properties[f"record:{k}"] = v
                
        all_links = record_data.get("links", []) + parent_links
        properties = self.enrich_properties(properties, all_links)
        
        if category:
            cat_keyword = category[:-1] if category.endswith('s') else category
            if "keywords" not in properties:
                properties["keywords"] = []
            if cat_keyword not in properties["keywords"]:
                properties["keywords"].append(cat_keyword)
            
        dt_obj = datetime.now(timezone.utc)
        if "datetime" in properties and properties["datetime"]:
            try:
                dt_obj = parse(properties["datetime"])
                if dt_obj.tzinfo is None: dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            except: pass
                
        # Determine target collection
        target_col_id = collection_id
        
        # If no explicit collection_id was passed, it's an experiment being processed directly
        if not target_col_id:
            workflow_ref = properties.get("osc:workflow") or properties.get("kb:workflow")
            if workflow_ref:
                # Map the experiment directly into its parent workflow collection
                if "workflow" not in workflow_ref.lower():
                    workflow_ref = f"{workflow_ref}-workflow"
                target_col_id = workflow_ref
            else:
                target_col_id = "experiments" # fallback
            
        # Ensure that if it's linking to a workflow, it uses the correct suffix
        # And rename osc:workflow to kb:workflow to comply with STAC OSC extension
        if "osc:workflow" in properties:
            wf_ref = properties.pop("osc:workflow")
            if "workflow" not in wf_ref.lower():
                wf_ref = f"{wf_ref}-workflow"
            properties["kb:workflow"] = wf_ref
        elif "kb:workflow" in properties:
            wf_ref = properties["kb:workflow"]
            if "workflow" not in wf_ref.lower():
                properties["kb:workflow"] = f"{wf_ref}-workflow"

        item = pystac.Item(id=record_id, geometry=geom, bbox=bbox, datetime=dt_obj, properties=properties)
        item.collection_id = target_col_id
        
        # Add self link pointing to source if needed
        # It will be processed further in prepare_links
        item.add_link(pystac.Link(rel="self", target=href, media_type="application/json"))
        
        if self.add_source_links:
            # Also add canonical link which is less likely to be rewritten by APIs
            item.add_link(pystac.Link(rel="canonical", target=href, media_type="application/json"))

        return item.to_dict()

    async def _async_process_and_ingest(self, session: aiohttp.ClientSession, sem: asyncio.Semaphore, href: str, links: List[Any], collection_id: Optional[str] = None, category: Optional[str] = None) -> int:
        async with sem:
            item_dict = await self._async_process_record(session, href, links, collection_id, category)
            if item_dict:
                target_col_id = item_dict.get("collection")
                if target_col_id in self.skip_collections:
                    return 0
                await self._async_ingest_entity(session, f"collections/{target_col_id}/items", item_dict)
                return 1
            return 0

    async def _async_find_and_ingest_items(self, session: aiohttp.ClientSession, sem: asyncio.Semaphore, catalog_link: pystac.Link, collection_links: List[Any], collection_id: str, visited: Set[str], depth: int = 0, category: Optional[str] = None) -> int:
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
                    task = asyncio.create_task(self._async_process_and_ingest(session, sem, item_href, catalog.links + collection_links, collection_id, category=category))
                    tasks.append(task)
            
            # Recurse into children synchronously, but gather their task counts
            for sub_link in catalog.get_child_links():
                items_processed += await self._async_find_and_ingest_items(session, sem, sub_link, collection_links, collection_id, visited, depth + 1, category=category)
                
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
            
            # 1. Directly linked items
            for item_link in collection.get_item_links():
                href = item_link.get_absolute_href()
                if href:
                    tasks.append(asyncio.create_task(self._async_process_and_ingest(session, sem, href, collection.links, collection.id, category=category)))
            
            # 2. Child links (item catalogs)
            for child_link in collection.get_child_links():
                child_href = child_link.get_absolute_href()
                if not child_href: continue
                
                is_internal = child_href.startswith(self.base_url)
                if is_internal or self.crawl_external:
                    if self.debug: console.print(f"      [dim]Starting item search in: {child_href}[/dim]")
                    items_ingested += await self._async_find_and_ingest_items(session, sem, child_link, collection.links, collection.id, visited=set(), depth=1, category=category)
            
            if tasks:
                results = await asyncio.gather(*tasks)
                items_ingested += sum(results)
                
            return items_ingested

    def collection_exists(self, collection_id: str) -> bool:
        try:
            resp = self.session.get(f"{self.eoapi_url}/collections/{collection_id}")
            return resp.status_code == 200
        except Exception:
            return False

    def _ingest_collection_and_items(self, collection: pystac.Collection, entity_link: pystac.Link, category: Optional[str] = None):
        if collection.id in self.skip_collections and self.collection_exists(collection.id):
            console.print(f"  [bold yellow][Skip] Collection {collection.id} already exists (in skip list).[/bold yellow]")
            self.stats["collections_skipped"] += 1
            return

        if category:
            cat_keyword = category[:-1] if category.endswith('s') else category
            keywords = collection.keywords or []
            if cat_keyword not in keywords:
                keywords.append(cat_keyword)
            collection.keywords = keywords

        start_time = time.time()
        col_dict = self.prepare_collection(collection, collection.links, source_url=entity_link.get_absolute_href())
        
        # Ingest the collection synchronously first
        success = self.ingest_entity("collections", col_dict)
        if not success:
            console.print(f"  [bold red][!] Aborting item ingestion for {collection.id} due to collection ingestion failure.[/bold red]")
            return

        if category in ["products", None]:
            self.stats["products_processed"] += 1
            
        console.print(f"  [+] Ingested Collection: [bold]{collection.id}[/bold]")
        
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        items_ingested = loop.run_until_complete(self._async_run_collection_ingestion(collection, entity_link, category))
        
        if category in ["products", None]:
            self.stats["product_items_processed"] += items_ingested
        elif category == "workflows":
            self.stats["workflows_processed"] += items_ingested
        elif category == "experiments":
            self.stats["experiments_processed"] += items_ingested
        
        elapsed_time = time.time() - start_time
        console.print(f"      └── Fetched and ingested [bold cyan]{items_ingested}[/bold cyan] items in [yellow]{elapsed_time:.2f}s[/yellow]")

    def crawl_catalog(self, catalog: pystac.Catalog, is_root: bool = False, is_external: bool = False):
        href = catalog.get_self_href()
        if href in self.visited_urls:
            return
        self.visited_urls.add(href)
        
        target_categories = self.categories
        
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
                        # 1. PRODUCTS: Standard collections linked as child
                        if category == "products":
                            for entity_link in child.get_child_links():
                                try:
                                    collection = entity_link.resolve_stac_object().target
                                    if isinstance(collection, pystac.Collection):
                                        self._ingest_collection_and_items(collection, entity_link, category)
                                except Exception as e:
                                    self.stats["fetch_errors"] += 1
                                    console.print(f"  [red]  [!] Failed processing collection {entity_link.href}: {e}[/red]")
                                
                        # 2. WORKFLOWS: Linked directly as items in the workflows catalog
                        elif category == "workflows":
                            for entity_link in child.get_item_links():
                                record_href = entity_link.get_absolute_href()
                                # Append -workflow if not already present
                                raw_id = record_href.split('/')[-2]
                                workflow_id = raw_id if "workflow" in raw_id.lower() else f"{raw_id}-workflow"
                                
                                if workflow_id in self.skip_collections and self.collection_exists(workflow_id):
                                    console.print(f"  [bold yellow][Skip] Workflow Collection {workflow_id} already exists.[/bold yellow]")
                                    self.stats["collections_skipped"] += 1
                                    continue

                                try:
                                    resp = self.session.get(record_href)
                                    if resp.status_code != 200: continue
                                    
                                    rec_data = resp.json()
                                    record_properties = rec_data.get("properties", {})
                                    
                                    title = record_properties.get("title", entity_link.title or raw_id)
                                    desc = record_properties.get("description", f"Workflow: {title}")
                                    
                                    props = {}
                                    for k, v in record_properties.items():
                                        if k not in ["title", "description", "type"]:
                                            props[k] = v
                                            
                                    # Enrich with KB
                                    all_links = rec_data.get("links", []) + child.links
                                    props = self.enrich_properties(props, all_links)
                                    
                                    extent = pystac.Extent(
                                        pystac.SpatialExtent([self.global_bbox]),
                                        pystac.TemporalExtent([[datetime(1970, 1, 1, tzinfo=timezone.utc), None]])
                                    )
                                    
                                    workflow_collection = pystac.Collection(
                                        id=workflow_id,
                                        title=title,
                                        description=desc,
                                        extent=extent,
                                        license="proprietary"
                                    )
                                    workflow_collection.extra_fields = props
                                    workflow_collection.keywords = ["workflow"]
                                    
                                    # Ingest Collection (Workflows have no items of their own)
                                    col_dict = self.prepare_collection(workflow_collection, all_links, source_url=record_href)
                                    if self.ingest_entity("collections", col_dict):
                                        console.print(f"  [+] Ingested Workflow Collection: [bold]{workflow_id}[/bold]")
                                        self.stats["workflows_processed"] += 1
                                    else:
                                        console.print(f"  [red]  [!] Failed to ingest workflow collection {workflow_id}[/red]")
                                    
                                except Exception as e:
                                    self.stats["fetch_errors"] += 1
                                    console.print(f"  [red]  [!] Failed processing workflow {record_href}: {e}[/red]")

                        # 3. EXPERIMENTS: Linked directly as items in the experiments catalog
                        elif category == "experiments":
                            item_links = list(child.get_item_links())
                            if item_links:
                                console.print(f"  [*] Processing Experiments as Items (Mapping to Workflow Collections)...")
                                
                                async def _ingest_experiments():
                                    connector = aiohttp.TCPConnector(limit=50)
                                    sem = asyncio.Semaphore(20)
                                    async with aiohttp.ClientSession(connector=connector) as session:
                                        tasks = []
                                        for entity_link in item_links:
                                            tasks.append(asyncio.create_task(
                                                self._async_process_and_ingest(session, sem, entity_link.get_absolute_href(), child.links, None, category="experiments")
                                            ))
                                        results = await asyncio.gather(*tasks) if tasks else []
                                        return sum(results)
                                        
                                try:
                                    loop = asyncio.get_event_loop()
                                except RuntimeError:
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    
                                start_time = time.time()
                                items_ingested = loop.run_until_complete(_ingest_experiments())
                                self.stats["experiments_processed"] += items_ingested
                                console.print(f"      └── Ingested [bold cyan]{items_ingested}[/bold cyan] experiments in [yellow]{time.time()-start_time:.2f}s[/yellow]")
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
        console.print("[bold]CRAWL SUMMARY[/bold]")
        console.print("=" * 50)
        console.print(f"Products Processed:    {self.stats['products_processed']}")
        console.print(f"Product Items:         {self.stats['product_items_processed']}")
        console.print(f"Workflows Ingested:    {self.stats['workflows_processed']}")
        console.print(f"Experiments Ingested:  {self.stats['experiments_processed']}")
        console.print(f"Collections Skipped:   {self.stats['collections_skipped']}")
        console.print(f"Successful API Posts:  {self.stats['api_success']}")
        console.print(f"API Errors (Failed):   {self.stats['api_errors']}")
        console.print(f"Fetch/Read Errors:     {self.stats['fetch_errors']}")
        console.print("=" * 50)
