import React from 'react';
import {ChapterTitle} from './components/ChapterTitle';
import {FlowDiagram} from './components/FlowDiagram';
import {MapRoute} from './components/MapRoute';
import {MapSplit} from './components/MapSplit';
import {RerouteMap} from './components/RerouteMap';
import {StatCard} from './components/StatCard';
import {TextOverlay} from './components/TextOverlay';
import {TextCard} from './components/TextCard';
import {HeroTitle} from './components/HeroTitle';
import {SectionTitle} from './components/SectionTitle';
import {StatCardV2} from './components/StatCardV2';
import {CalloutBox} from './components/CalloutBox';
import {ComparisonCard} from './components/ComparisonCard';
import {AnimeScene} from './components/AnimeScene';
import {TerminalScene} from './components/TerminalScene';
import {ScreenshotScene} from './components/ScreenshotScene';
import {TitledVideo} from './components/TitledVideo';
import {ProductReveal} from './components/ProductReveal';
import {CollageBurst} from './components/CollageBurst';
import {LyricOverlay} from './components/LyricOverlay';
import {BarChart} from './components/charts/BarChart';
import {LineChart} from './components/charts/LineChart';
import {PieChart} from './components/charts/PieChart';
import {KPIGrid} from './components/charts/KPIGrid';
import {StatReveal} from './components/charts/StatReveal';
import {ProgressBar} from './components/charts/ProgressBar';
import {CaptionOverlay} from './components/charts/CaptionOverlay';

const templateRegistry: Record<string, React.FC<any>> = {
  ChapterTitle,
  FlowDiagram,
  MapRoute,
  MapSplit,
  RerouteMap,
  StatCard,
  TextOverlay,
  TextCard,
  HeroTitle,
  SectionTitle,
  StatCardV2,
  CalloutBox,
  ComparisonCard,
  AnimeScene,
  TerminalScene,
  ScreenshotScene,
  TitledVideo,
  ProductReveal,
  CollageBurst,
  LyricOverlay,
  BarChart,
  LineChart,
  PieChart,
  KPIGrid,
  StatReveal,
  ProgressBar,
  CaptionOverlay,
};

export function getTemplate(name: string): React.FC<any> | null {
  return templateRegistry[name] ?? null;
}

export function registerTemplate(name: string, component: React.FC<any>): void {
  templateRegistry[name] = component;
}

export function listTemplates(): string[] {
  return Object.keys(templateRegistry);
}
