export interface ThemeConfig {
  primaryColor: string;
  accentColor: string;
  backgroundColor: string;
  surfaceColor: string;
  textColor: string;
  mutedTextColor: string;
  chartColors: string[];
  captionHighlightColor: string;
  captionBackgroundColor: string;
}

export const THEMES: Record<string, ThemeConfig> = {
  'flat-motion-graphics': {
    primaryColor: '#7C3AED',
    accentColor: '#EC4899',
    backgroundColor: '#0F172A',
    surfaceColor: '#1E293B',
    textColor: '#F8FAFC',
    mutedTextColor: '#94A3B8',
    chartColors: ['#7C3AED', '#EC4899', '#06B6D4', '#F59E0B', '#10B981', '#EF4444'],
    captionHighlightColor: '#22D3EE',
    captionBackgroundColor: 'rgba(15, 23, 42, 0.75)',
  },
  'clean-professional': {
    primaryColor: '#2563EB',
    accentColor: '#F59E0B',
    backgroundColor: '#FFFFFF',
    surfaceColor: '#F9FAFB',
    textColor: '#1F2937',
    mutedTextColor: '#6B7280',
    chartColors: ['#2563EB', '#F59E0B', '#10B981', '#8B5CF6', '#EC4899', '#06B6D4'],
    captionHighlightColor: '#2563EB',
    captionBackgroundColor: 'rgba(255, 255, 255, 0.85)',
  },
};

export const DEFAULT_THEME = THEMES['flat-motion-graphics'];

export function resolveTheme(themeName?: string): ThemeConfig {
  if (themeName && THEMES[themeName]) return THEMES[themeName];
  return DEFAULT_THEME;
}
