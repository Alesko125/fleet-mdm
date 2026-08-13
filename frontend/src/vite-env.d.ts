/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Власний тайловий сервер карти (для розгортання без звернень до OSM). */
  readonly VITE_MAP_TILE_URL?: string;
  readonly VITE_MAP_TILE_ATTRIBUTION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
