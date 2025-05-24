"""Hole utilities for kfactory.

This module provides utilities for creating holes in layouts using tiling processors.
"""

from typing import List, Tuple, Optional

import kfactory as kf
from kfactory import kdb
from kfactory.layout import KCLayout
from kfactory.kcell import KCell
from kfactory.conf import config, logger


class HoleProcessor(kdb.TileOutputReceiver):
    """Output Receiver of the TilingProcessor for hole creation.
    
    This class handles the creation of holes in a layout using a tiling processor.
    It manages the transformation and placement of hole patterns within specified
    regions while respecting margins and spacing requirements.
    """

    def __init__(
        self,
        kcl: KCLayout,
        top_cell: KCell,
        hole_cell_index: int,
        hc_bbox: kdb.Box,
        row_step: kdb.Vector,
        column_step: kdb.Vector,
        hole_margin: Optional[kdb.Vector] = None,
    ) -> None:
        """Initialize the hole processor.
        
        Args:
            kcl: The KCLayout instance
            top_cell: The top-level cell to process
            hole_cell_index: Index of the hole cell in the layout
            hc_bbox: Bounding box of the hole cell
            row_step: Step size for rows
            column_step: Step size for columns
            hole_margin: Optional margin around holes
        """
        if hole_margin is None:
            hole_margin = kdb.Vector(0, 0)
            
        self.kcl = kcl
        self.top_cell = top_cell
        self.hole_cell_index = hole_cell_index
        self.hc_bbox = hc_bbox
        self.row_step = row_step
        self.column_step = column_step
        self.hole_margin = hole_margin
        self.holed_cells: List[kdb.Cell] = []
        
        # create a temporary layout to work with
        # this helps avoid modifying the original layout during processing
        self.temp_ly = kdb.Layout()
        self.temp_tc = self.temp_ly.create_cell(top_cell.name)
        
        # copy the hole cell to our temporary layout
        # we need this to create the hole pattern
        hc = kcl.layout.cell(hole_cell_index)
        self.temp_hc = self.temp_ly.create_cell(hc.name)
        self.temp_hc_ind = self.temp_hc.cell_index()
        self.temp_hc.copy_shapes(hc)
        
        # start making changes to the layout
        self.temp_ly.start_changes()

    def put(
        self,
        ix: int,
        iy: int,
        tile: kdb.Box,
        region: kdb.Region,
        dbu: float,
        clip: bool,
    ) -> None:
        """Process a tile in the layout.
        
        This method is called by the TilingProcessor for each tile in the layout.
        It creates holes in the specified region according to the tile parameters.
        
        Args:
            ix: X index of the tile
            iy: Y index of the tile
            tile: Bounding box of the tile
            region: Region to process
            dbu: Database units
            clip: Whether to clip the result
        """
        # fill the region with holes using the hole cell
        # this creates the actual hole pattern in our temporary layout
        self.temp_tc.fill_region(
            region=region,
            fill_cell_index=self.temp_hc_ind,
            fc_bbox=self.hc_bbox,
            row_step=self.row_step,
            column_step=self.column_step,
            origin=tile.p1,
            remaining_parts=None,
            fill_margin=self.hole_margin,
            remaining_polygons=None,
            glue_box=tile,
        )

    def insert_holes(self) -> None:
        """Insert holes into the processed regions.
        
        This method finalizes the hole creation process by:
        1. Ending the layout changes
        2. Getting the target layer from the hole cell
        3. Creating the hole pattern
        4. Subtracting holes from the original region
        5. Inserting the final result
        """
        # finish making changes to the temporary layout
        self.temp_ly.end_changes()
        
        # find the first layer in the hole cell that has shapes
        # this will be our target layer for creating holes
        hole_cell = self.kcl.layout.cell(self.hole_cell_index)
        target_layer = None
        for layer in hole_cell.layout().layer_indices():
            if hole_cell.shapes(layer).size() > 0:
                target_layer = layer
                break
        
        # if no shapes found, we can't create holes
        if target_layer is None:
            logger.warning("no shapes found in hole cell")
            return
            
        # get all the holes we created in the temporary layout
        # this is our hole pattern that we'll subtract from the original
        hole_pattern = kdb.Region(self.temp_tc.begin_shapes_rec(target_layer))
        
        # get the original shapes from the target layer
        # these are the shapes we'll be making holes in
        original_region = kdb.Region(self.top_cell.begin_shapes_rec(target_layer))
        
        # subtract the holes from the original shapes
        # this creates the final result with holes
        final_region = original_region - hole_pattern
        
        # clear the target layer and insert our final result
        # this updates the original layout with the holes
        self.top_cell.shapes(target_layer).clear()
        self.top_cell.shapes(target_layer).insert(final_region)


def hole_tiled(
    c: kf.KCell,
    hole_cell: kf.KCell,
    hole_layers: list[tuple[kdb.LayerInfo, int]] = [],
    hole_regions: list[tuple[kdb.Region, int]] = [],
    exclude_layers: list[tuple[kdb.LayerInfo, int]] = [],
    exclude_regions: list[tuple[kdb.Region, int]] = [],
    x_space: float = 0,
    y_space: float = 0,
) -> None:
    """Tile holes in the specified regions/layers of a KCell.
    
    Args:
        c: Target cell.
        hole_cell: The cell used as a hole (subtracted from the region).
        hole_layers: Tuples of layer and keepout w.r.t. the regions to hole.
        hole_regions: Specific regions to hole. Also tuples like the layers.
        exclude_layers: Layers to ignore. Tuples like the hole layers.
        exclude_regions: Specific regions to ignore. Tuples like the hole layers.
        x_space: Spacing between the hole cell bounding boxes in x-direction.
        y_space: Spacing between the hole cell bounding boxes in y-direction.
    """
    # create a tiling processor to handle the hole creation
    # this will help us efficiently process large layouts
    tp = kdb.TilingProcessor()
    
    # calculate the total area we need to process
    # this includes any specific regions we want to hole
    dbb = c.dbbox()
    for r, ext in hole_regions:
        dbb += r.bbox().to_dtype(c.kcl.dbu).enlarged(ext)
    tp.frame = dbb  # type: ignore[assignment, misc]
    tp.dbu = c.kcl.dbu
    tp.threads = config.n_threads

    # figure out how big each tile should be
    # we want tiles big enough to fit our hole pattern plus spacing
    tile_size = (
        100 * (hole_cell.dbbox().width() + x_space),
        100 * (hole_cell.dbbox().height() + y_space),
    )
    tp.tile_size(*tile_size)
    tp.tile_border(20, 20)  # add some border to make sure holes line up right

    # set up all the layers we want to process
    # these are the layers we'll be making holes in
    layer_names: list[str] = []
    for _layer, _ in hole_layers:
        layer_name = (
            f"layer{_layer.name}"
            if _layer.is_named()
            else f"layer_{_layer.layer}_{_layer.datatype}"
        )
        tp.input(layer_name, c.kcl.layout, c.cell_index(), _layer)
        layer_names.append(layer_name)

    # set up any specific regions we want to process
    # these are areas where we definitely want holes
    region_names: list[str] = []
    for i, (r, _) in enumerate(hole_regions):
        region_name = f"region{i}"
        tp.input(region_name, r)
        region_names.append(region_name)

    # set up layers we want to exclude
    # these are areas where we don't want any holes
    exlayer_names: list[str] = []
    for _layer, _ in exclude_layers:
        layer_name = (
            f"layer{_layer.name}"
            if _layer.is_named()
            else f"layer_{_layer.layer}_{_layer.datatype}"
        )
        tp.input(layer_name, c.kcl.layout, c.cell_index(), _layer)
        exlayer_names.append(layer_name)

    # set up regions we want to exclude
    # these are specific areas where we don't want holes
    exregion_names: list[str] = []
    for i, (r, _) in enumerate(exclude_regions):
        region_name = f"region{i}"
        tp.input(region_name, r)
        exregion_names.append(region_name)

    # calculate how far to step between holes
    # this determines the spacing between holes in the pattern
    row_step = kdb.Vector(hole_cell.bbox().width() + int(x_space / c.kcl.dbu), 0)
    col_step = kdb.Vector(0, hole_cell.bbox().height() + int(y_space / c.kcl.dbu))
    hc_bbox = hole_cell.bbox()

    # create our hole processor
    # this will handle the actual hole creation for each tile
    operator = HoleProcessor(
        c.kcl,
        c,
        hole_cell.cell_index(),
        hc_bbox=hc_bbox,
        row_step=row_step,
        column_step=col_step,
    )
    tp.output("to_hole", operator)

    # build the processing string for the tiling processor
    # this tells it how to create the holes and what to exclude
    if layer_names or region_names:
        # combine all the exclude layers and regions
        # we'll use these to avoid making holes in certain areas
        exlayers = " + ".join(
            [
                layer_name + f".sized({c.kcl.to_dbu(size)})" if size else layer_name
                for layer_name, (_, size) in zip(
                    exlayer_names, exclude_layers, strict=False
                )
            ]
        )
        exregions = " + ".join(
            [
                region_name + f".sized({c.kcl.to_dbu(size)})" if size else region_name
                for region_name, (_, size) in zip(
                    exregion_names, exclude_regions, strict=False
                )
            ]
        )
        
        # combine all the layers and regions we want to hole
        # these are the areas where we'll create holes
        layers = " + ".join(
            [
                layer_name + f".sized({c.kcl.to_dbu(size)})" if size else layer_name
                for layer_name, (_, size) in zip(layer_names, hole_layers, strict=False)
            ]
        )
        regions = " + ".join(
            [
                region_name + f".sized({c.kcl.to_dbu(size)})" if size else region_name
                for region_name, (_, size) in zip(
                    region_names, hole_regions, strict=False
                )
            ]
        )

        # build the final processing string
        # this tells the tiling processor exactly what to do
        if exlayer_names or exregion_names:
            queue_str = (
                "var hole= "
                + (f"{layers} + {regions}" if regions and layers else regions + layers)
                + "; var exclude = "
                + (
                    f"{exlayers} + {exregions}"
                    if exregions and exlayers
                    else exregions + exlayers
                )
                + "; var hole_region = _tile.minkowski_sum(Box.new("
                f"0,0,{hc_bbox.width() - 1},{hc_bbox.height() - 1}))"
                " & _frame & hole - exclude; _output(to_hole, hole_region)"
            )
        else:
            queue_str = (
                "var hole= "
                + (f"{layers} + {regions}" if regions and layers else regions + layers)
                + "; var hole_region = _tile.minkowski_sum(Box.new("
                f"0,0,{hc_bbox.width() - 1},{hc_bbox.height() - 1}))"
                " & _frame & hole;"
                " _output(to_hole, hole_region)"
            )
        tp.queue(queue_str)
        c.kcl.start_changes()
        try:
            logger.debug(
                "creating holes in {} with {}", c.kcl.future_cell_name or c.name, hole_cell.name
            )
            logger.debug("hole string: '{}'", queue_str)
            tp.execute(f"hole {c.name}")
            logger.info("done with calculating hole regions for {}", c.name)
            operator.insert_holes()
        finally:
            c.kcl.end_changes() 