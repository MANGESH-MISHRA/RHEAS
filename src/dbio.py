""" Module for interfacing with the PostGIS database

.. module:: dbio
   :synopsis: Definition of the DBIO module

.. moduleauthor:: Kostas Andreadis <kandread@jpl.nasa.gov>

"""

import numpy as np
import tempfile
from osgeo import gdal, osr
import subprocess
import random
import psycopg2 as pg
import string
import rpath
import sys


def connect(dbname):
    """Connect to database *dbname*."""
    try:
        db = pg.connect(database=dbname)
    except pg.OperationalError:
        db = None
        try:
            db = pg.connect(database=dbname, host="/tmp/")
        except:
            print("Cannot connect to database {0}. Please restart it by running \n {1}/pg_ctl -D {2}/postgres restart".format(
                dbname, rpath.bins, rpath.data))
            sys.exit()
    return db


def writeGeotif(lat, lon, res, data, filename=None):
    """Writes Geotif in temporary directory so it can be imported into the PostGIS database."""
    if isinstance(data, np.ma.masked_array):
        nodata = np.double(data.fill_value)
        data = data.data
    else:
        nodata = -9999.
    if len(data.shape) < 2:
        nrows = int((max(lat) - min(lat)) / res) + 1
        ncols = int((max(lon) - min(lon)) / res) + 1
        out = np.zeros((nrows, ncols)) + nodata
        for c in range(len(lat)):
            i = int((max(lat) - lat[c]) / res)
            j = int((lon[c] - min(lon)) / res)
            out[i, j] = data[c]
    else:
        nrows, ncols = data.shape
        out = data
    if filename is None:
        f = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        filename = f.name
        f.close()
    driver = gdal.GetDriverByName("GTiff")
    ods = driver.Create(filename, ncols, nrows, 1, gdal.GDT_Float32)
    ods.SetGeoTransform([min(lon) - res / 2.0, res, 0,
                         max(lat) + res / 2.0, 0, -res])
    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS("WGS84")
    ods.SetProjection(srs.ExportToWkt())
    ods.GetRasterBand(1).WriteArray(out)
    ods.GetRasterBand(1).SetNoDataValue(nodata)
    ods = None
    return filename


def _getResamplingMethod(dbname, tablename, res):
    """Return a raster resampling method based on the resolution of the model and the requested datasets."""
    db = connect(dbname)
    cur = db.cursor()
    cur.execute(
        "select st_pixelheight(rast) from {0} limit 1".format(tablename))
    data_res = cur.fetchone()[0]
    if res == data_res:
        resample_method = "near"
    elif res < data_res:
        resample_method = "bilinear"
    else:
        resample_method = "average"
    cur.close()
    db.close()
    return resample_method


def _createRasterTable(dbname, stname):
    """Create table *stname* holding rasters in database *dbname*."""
    db = connect(dbname)
    cur = db.cursor()
    cur.execute(
        "create table {0} (rid serial primary key, rast raster, fdate date not null)".format(stname))
    db.commit()
    cur.close()
    db.close()


def _createResampledViews(dbname, sname, tname, temptable, dt, tilesize):
    """Cache resampled tables by using materialized views."""
    db = connect(dbname)
    cur = db.cursor()
    # create catalog that holds information on resampled rasters
    sql = """create or replace function resampled(_s text, _t text, out result double precision) as
    $func$
    begin
    execute format('select st_scalex(rast) from %s.%s limit 1',quote_ident(_s),quote_ident(_t)) into result;
    end
    $func$ language plpgsql;"""
    cur.execute(sql)
    cur.execute("create or replace view raster_resampled as (select r_table_schema as sname,r_table_name as tname,resampled(r_table_schema,r_table_name) as resolution from raster_columns)")
    # create or update materialized view for each resolution available to VIC
    cur.execute("select distinct(resolution) from vic.soils")
    if bool(cur.rowcount):
        resolutions = [r[0] for r in cur.fetchall()]
        for res in resolutions:
            # check if view exists
            cur.execute(
                "select * from pg_catalog.pg_class c inner join pg_catalog.pg_namespace n on c.relnamespace=n.oid where n.nspname='{0}' and c.relname='{1}_{2}'".format(sname, tname, int(1.0 / res)))
            method = _getResamplingMethod(
                dbname, "{0}.{1}".format(sname, tname), res)
            # if it exists just refresh it, if not create it
            if bool(cur.rowcount):
                sql = "insert into {0}.{1}_{2} (with dt as (select max(fdate) as maxdate from {0}.{1}_{2}), f as (select fdate,st_tile(st_rescale(rast,{3},'{4}'),{5},{6}) as rast from {0}.{1},dt where fdate>maxdate) select fdate,rast,dense_rank() over (order by st_upperleftx(rast),st_upperlefty(rast)) as rid from f)".format(sname, tname, int(1.0 / res), res, method, tilesize[0], tilesize[1])
                cur.execute(sql)
                # cur.execute("refresh materialized view {0}.{1}_{2}".format(
                #     sname, tname, int(1.0 / res)))
            else:
                sql = "create table {0}.{1}_{2} as (with f as (select fdate,st_tile(st_rescale(rast,{3},'{4}'),{5},{6}) as rast from {0}.{1}) select fdate,rast,dense_rank() over (order by st_upperleftx(rast),st_upperlefty(rast)) as rid from f)".format(
                    sname, tname, int(1.0 / res), res, method, tilesize[0], tilesize[1])
                # sql = "create materialized view {0}.{1}_{2} as (with f as (select fdate,st_tile(st_rescale(rast,{3},'{4}'),{5},{6}) as rast from {0}.{1}) select fdate,rast,dense_rank() over (order by st_upperleftx(rast),st_upperlefty(rast)) as rid from f)".format(
                #     sname, tname, int(1.0 / res), res, method, tilesize[0], tilesize[1])
                cur.execute(sql)
                cur.execute("create index {1}_{2}_t on {0}.{1}_{2}(fdate)".format(
                    sname, tname, int(1.0 / res)))
                cur.execute("create index {1}_{2}_r on {0}.{1}_{2}(rid)".format(
                    sname, tname, int(1.0 / res)))
        db.commit()


def ingest(dbname, filename, dt, stname, resample=True):
    """Imports Geotif *filename* into database *db*."""
    tilesize = (10, 10)
    db = connect(dbname)
    cur = db.cursor()
    # import temporary table
    temptable = ''.join(random.SystemRandom().choice(
        string.ascii_letters) for _ in range(8))
    subprocess.call("{3}/raster2pgsql -d -s 4326 {0} {2} | {3}/psql -d {1}".format(
        filename, dbname, temptable, rpath.bins), shell=True)
    cur.execute("alter table {0} add column fdate date".format(temptable))
    cur.execute(
        "update {3} set fdate = date '{0}-{1}-{2}'".format(dt.year, dt.month, dt.day, temptable))
    # check if table exists
    schemaname, tablename = stname.split(".")
    cur.execute(
        "select * from information_schema.schemata where schema_name='{0}'".format(schemaname))
    if not bool(cur.rowcount):
        cur.execute("create schema {0}".format(schemaname))
    cur.execute(
        "select * from information_schema.tables where table_schema='{0}' and table_name='{1}'".format(schemaname, tablename))
    if not bool(cur.rowcount):
        _createRasterTable(dbname, stname)
    # create tiles from imported raster and insert into table
    cur.execute("insert into {0}.{1} (fdate,rast) select fdate,rast from {2}".format(
        schemaname, tablename, temptable))
    # create indexes for table
    cur.execute("drop index if exists {0}.{1}_t".format(schemaname, tablename))
    cur.execute("create index {1}_t on {0}.{1}(fdate)".format(
        schemaname, tablename))
    db.commit()
    # create materialized views for resampled rasters
    if resample:
        _createResampledViews(dbname, schemaname,
                              tablename, temptable, dt, tilesize)
    # delete temporary table
    cur.execute("drop table {0}".format(temptable))
    db.commit()
    cur.close()
    db.close()
