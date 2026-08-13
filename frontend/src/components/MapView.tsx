import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { useEffect, useRef } from 'react';
import type { LocationFix } from '../api/types';

/**
 * Тайли за замовчуванням беруться з OpenStreetMap. Це означає, що браузер
 * оператора звертається до стороннього сервера — приблизний район пристрою
 * стає видимим провайдеру тайлів. Для повністю ізольованого розгортання
 * задайте власний тайловий сервер через VITE_MAP_TILE_URL.
 */
const TILE_URL =
  import.meta.env.VITE_MAP_TILE_URL ?? 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
const TILE_ATTRIBUTION =
  import.meta.env.VITE_MAP_TILE_ATTRIBUTION ?? '© OpenStreetMap contributors';

interface Props {
  fixes: LocationFix[];
  height?: number;
  selectedId?: string | null;
  onSelect?: (fix: LocationFix) => void;
}

export default function MapView({ fixes, height = 380, selectedId, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);

  // Ініціалізація карти — один раз.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, { attributionControl: true }).setView([50.45, 30.52], 11);
    L.tileLayer(TILE_URL, { attribution: TILE_ATTRIBUTION, maxZoom: 19 }).addTo(map);
    layerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
      layerRef.current = null;
    };
  }, []);

  // Перемальовуємо точки й маршрут при зміні даних.
  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer) return;

    layer.clearLayers();
    if (fixes.length === 0) return;

    // Найсвіжіша точка приходить першою — для маршруту потрібен хронологічний порядок.
    const ordered = [...fixes].reverse();
    const points: [number, number][] = ordered.map((fix) => [fix.latitude, fix.longitude]);

    if (points.length > 1) {
      L.polyline(points, { color: '#4c8dff', weight: 3, opacity: 0.7 }).addTo(layer);
    }

    ordered.forEach((fix, index) => {
      const isLatest = index === ordered.length - 1;
      const isSelected = selectedId === fix.id;
      const marker = L.circleMarker([fix.latitude, fix.longitude], {
        radius: isLatest || isSelected ? 9 : 5,
        color: isSelected ? '#e5a13a' : isLatest ? '#35c07f' : '#4c8dff',
        fillColor: isSelected ? '#e5a13a' : isLatest ? '#35c07f' : '#4c8dff',
        fillOpacity: 0.85,
        weight: 2,
      }).addTo(layer);

      const accuracy = fix.accuracy_m ? `±${Math.round(fix.accuracy_m)} м` : 'точність невідома';
      marker.bindTooltip(
        `${new Date(fix.recorded_at).toLocaleString('uk-UA')}<br/>${accuracy} · ${
          fix.provider ?? '—'
        }`,
      );
      if (onSelect) marker.on('click', () => onSelect(fix));

      // Коло точності робить зрозумілим, наскільки координата надійна.
      if (fix.accuracy_m && fix.accuracy_m > 0 && (isLatest || isSelected)) {
        L.circle([fix.latitude, fix.longitude], {
          radius: fix.accuracy_m,
          color: '#4c8dff',
          fillColor: '#4c8dff',
          fillOpacity: 0.08,
          weight: 1,
        }).addTo(layer);
      }
    });

    const bounds = L.latLngBounds(points);
    map.fitBounds(bounds, { padding: [30, 30], maxZoom: 16 });
  }, [fixes, selectedId, onSelect]);

  // Центруємо на обраній точці таймлайну.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selectedId) return;
    const fix = fixes.find((item) => item.id === selectedId);
    if (fix) map.setView([fix.latitude, fix.longitude], Math.max(map.getZoom(), 15));
  }, [selectedId, fixes]);

  return <div ref={containerRef} className="map" style={{ height }} />;
}
