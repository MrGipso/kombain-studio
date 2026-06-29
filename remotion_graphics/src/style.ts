import type React from 'react';

export const palette = {
  background: '#0b1020',
  panel: '#111827',
  panelAlt: '#172033',
  text: '#f8fafc',
  muted: '#94a3b8',
  accent: '#38bdf8',
  accent2: '#22c55e',
  warning: '#f97316',
  danger: '#ef4444',
  line: '#475569',
};

export const fontFamily = 'Inter, Segoe UI, Arial, sans-serif';

export const baseScene: React.CSSProperties = {
  width: '100%',
  height: '100%',
  background: palette.background,
  color: palette.text,
  fontFamily,
  position: 'relative',
  overflow: 'hidden',
};
