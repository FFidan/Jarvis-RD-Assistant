import { useEffect, useRef, useCallback } from 'react';
import cytoscape, { type Core, type LayoutOptions } from 'cytoscape';

export interface CytoscapeNode {
  id: string;
  label: string;
  type?: string;
  size?: number;
  metadata?: Record<string, unknown>;
}

export interface CytoscapeEdge {
  source: string;
  target: string;
  label?: string;
  directed?: boolean;
}

export interface CytoscapeGraphProps {
  nodes: CytoscapeNode[];
  edges: CytoscapeEdge[];
  layout?: 'cose' | 'breadthfirst' | 'circle' | 'concentric';
  colorMap?: Record<string, string>;
  height?: number;
  onNodeClick?: (nodeId: string) => void;
}

const DEFAULT_COLOR = '#6b7280';

export function CytoscapeGraph({
  nodes,
  edges,
  layout = 'cose',
  colorMap = {},
  height = 500,
  onNodeClick,
}: CytoscapeGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);

  const handleNodeClick = useCallback(
    (nodeId: string) => {
      onNodeClick?.(nodeId);
    },
    [onNodeClick],
  );

  useEffect(() => {
    if (!containerRef.current) return;

    const elements = [
      ...nodes.map((n) => ({
        data: {
          id: n.id,
          label: n.label.length > 25 ? n.label.slice(0, 22) + '...' : n.label,
          fullLabel: n.label,
          type: n.type ?? '',
          nodeSize: n.size ?? 20,
          color: (n.type && colorMap[n.type]) || DEFAULT_COLOR,
          ...n.metadata,
        },
      })),
      ...edges.map((e, i) => ({
        data: {
          id: `e-${i}`,
          source: e.source,
          target: e.target,
          label: e.label ?? '',
          directed: e.directed ?? true,
        },
      })),
    ];

    const layoutOptions: LayoutOptions =
      layout === 'cose'
        ? {
            name: 'cose',
            animate: false,
            nodeRepulsion: () => 8000,
            idealEdgeLength: () => 120,
            gravity: 0.25,
          }
        : layout === 'breadthfirst'
          ? { name: 'breadthfirst', directed: true, spacingFactor: 1.2 }
          : layout === 'concentric'
            ? {
                name: 'concentric',
                concentric: (n: cytoscape.NodeSingular) => n.degree(false),
                levelWidth: () => 2,
              }
            : { name: 'circle' };

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      layout: layoutOptions,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'background-color': 'data(color)',
            width: 'data(nodeSize)',
            height: 'data(nodeSize)',
            'font-size': 9,
            'text-valign': 'bottom',
            'text-margin-y': 4,
            color: '#555',
            'text-wrap': 'ellipsis',
            'text-max-width': '80px',
            'border-width': 1,
            'border-color': '#333',
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.5,
            'line-color': '#bbb',
            'target-arrow-color': '#bbb',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'font-size': 7,
            label: 'data(label)',
            'text-rotation': 'autorotate',
            color: '#888',
          },
        },
        {
          selector: 'edge[?directed=false]',
          style: {
            'target-arrow-shape': 'none',
          },
        },
        {
          selector: 'node:active',
          style: {
            'overlay-opacity': 0.1,
          },
        },
      ],
      userZoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
      minZoom: 0.2,
      maxZoom: 4,
    });

    cy.on('tap', 'node', (evt) => {
      const nodeId = evt.target.id();
      handleNodeClick(nodeId);
    });

    cyRef.current = cy;

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [nodes, edges, layout, colorMap, handleNodeClick, height]);

  if (nodes.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-md border bg-muted/30 text-muted-foreground"
        style={{ height }}
        data-testid="cytoscape-empty"
      >
        No graph data to display
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-testid="cytoscape-container"
      className="w-full rounded-md border bg-white dark:bg-zinc-950"
      style={{ height }}
    />
  );
}
