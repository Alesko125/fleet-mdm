import qrcode from 'qrcode-generator';
import { useMemo } from 'react';

/**
 * QR-код як inline-SVG.
 *
 * Кольори навмисно фіксовані (темні модулі на білому), а не з теми панелі:
 * сканери розраховують саме на такий контраст, і в темній темі інвертований
 * код читається гірше або не читається зовсім. Біла рамка навколо — це
 * обов'язкова «тиха зона» стандарту, без неї сканер не знаходить межі коду.
 */
export function QrCode({ value, size = 220 }: { value: string; size?: number }) {
  const path = useMemo(() => {
    // Тип 0 — автовибір найменшої версії, що вміщує дані.
    // Рівень корекції M: 15% пошкоджень, розумний компроміс для екрана.
    const qr = qrcode(0, 'M');
    qr.addData(value);
    qr.make();

    const count = qr.getModuleCount();
    const parts: string[] = [];
    for (let row = 0; row < count; row += 1) {
      for (let col = 0; col < count; col += 1) {
        if (qr.isDark(row, col)) {
          parts.push(`M${col} ${row}h1v1h-1z`);
        }
      }
    }
    return { d: parts.join(''), count };
  }, [value]);

  const quiet = 4;
  const side = path.count + quiet * 2;

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${side} ${side}`}
      role="img"
      aria-label="QR-код для реєстрації пристрою"
      shapeRendering="crispEdges"
    >
      <rect width={side} height={side} fill="#ffffff" />
      <g transform={`translate(${quiet} ${quiet})`} fill="#0b1220">
        <path d={path.d} />
      </g>
    </svg>
  );
}
