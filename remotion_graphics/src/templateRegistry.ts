import React from 'react';
import {ChapterTitle} from './components/ChapterTitle';
import {FlowDiagram} from './components/FlowDiagram';
import {MapRoute} from './components/MapRoute';
import {MapSplit} from './components/MapSplit';
import {RerouteMap} from './components/RerouteMap';
import {StatCard} from './components/StatCard';
import {TextOverlay} from './components/TextOverlay';

const templateRegistry: Record<string, React.FC<any>> = {
  ChapterTitle,
  FlowDiagram,
  MapRoute,
  MapSplit,
  RerouteMap,
  StatCard,
  TextOverlay,
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
