/*
 * pve-gpu-stats - GPU charts in the Proxmox VE node Summary.
 *
 * The data comes from /pve2/js/gpu-stats-<timeframe>-<cf>.json, written every
 * minute by the pve-gpu-stats service of the node. The charts follow the
 * timeframe selector of the Summary like the built-in ones.
 *
 * No 'use strict' here: ExtJS' callParent() does not work in strict mode.
 */
(function () {
    // Charts to add, in this order. Remove a line to hide a chart.
    var CHARTS = [
        { metric: 'util', title: 'GPU Usage', unit: 'percent', suffix: '%' },
        { metric: 'mem', title: 'GPU Memory', unit: 'bytes', powerOfTwo: true },
        { metric: 'temp', title: 'GPU Temperature', axisTitle: '°C', suffix: ' °C' },
        { metric: 'power', title: 'GPU Power', axisTitle: 'W', suffix: ' W' },
    ];

    // 'cpu': right after the "CPU Usage" chart, 'end': after the built-in charts.
    var POSITION = 'cpu';

    var BASE_URL = '/pve2/js/gpu-stats-';
    var STATE_ID = 'proxmoxRRDTypeSelection';
    var TIMEFRAMES = ['hour', 'day', 'week', 'month', 'year'];
    // one fixed color per GPU, whatever the timeframe shows
    var COLORS = [
        '#94ae0a', '#115fa6', '#a61120', '#ff8809', '#ffd13e',
        '#a61187', '#24ad9a', '#7c7474', '#a66111',
    ];

    if (typeof Ext === 'undefined' || !Ext.ClassManager.get('PVE.node.Summary')) {
        return;
    }

    function warn(err) {
        if (window.console && console.warn) {
            console.warn('pve-gpu-stats:', err);
        }
    }

    function dataUrl(state) {
        var timeframe = state && TIMEFRAMES.indexOf(state.timeframe) !== -1 ? state.timeframe : 'hour';
        var cf = state && state.cf === 'MAX' ? 'MAX' : 'AVERAGE';
        return BASE_URL + timeframe + '-' + cf + '.json';
    }

    function formatValue(def, value, gpu) {
        if (def.metric === 'mem') {
            var text = Proxmox.Utils.format_size(value);
            if (gpu.mem_total) {
                text += ' / ' + Proxmox.Utils.format_size(gpu.mem_total);
            }
            return text;
        }
        return Ext.util.Format.number(value, '0.#') + (def.suffix || '');
    }

    function chartConfig(def, gpus, store, columnWidth) {
        var byField = {};
        var fields = [];
        var titles = [];
        var colors = [];
        gpus.forEach(function (gpu) {
            var field = gpu.id + '_' + def.metric;
            byField[field] = gpu;
            fields.push(field);
            titles.push(gpu.label);
            colors.push(COLORS[(parseInt(String(gpu.id).slice(1), 10) || 0) % COLORS.length]);
        });

        return {
            xtype: 'proxmoxRRDChart',
            title: def.title,
            fields: fields,
            fieldTitles: titles,
            colors: colors,
            unit: def.unit,
            powerOfTwo: !!def.powerOfTwo,
            store: store,
            columnWidth: columnWidth,
            seriesConfig: {
                tooltip: {
                    trackMouse: true,
                    renderer: function (tooltip, record, item) {
                        var gpu = byField[item.field] || {};
                        var value = record.get(item.field);
                        if (value === null || value === undefined) {
                            tooltip.setHtml(gettext('No Data'));
                            return;
                        }
                        tooltip.setHtml(
                            Ext.htmlEncode(gpu.label || item.field) + ': ' +
                                formatValue(def, value, gpu) + '<br>' +
                                new Date(record.get('time')),
                        );
                    },
                },
            },
        };
    }

    function insertIndex(container) {
        if (POSITION === 'cpu') {
            var cpu = container.items.findBy(function (item) {
                return Ext.isArray(item.fields) && item.fields.indexOf('cpu') !== -1;
            });
            if (cpu) {
                return container.items.indexOf(cpu) + 1;
            }
        }
        return container.items.getCount();
    }

    function attach(summary) {
        var nodename = summary.pveSelNode && summary.pveSelNode.data && summary.pveSelNode.data.node;
        var container = summary.getComponent('itemcontainer') || summary.down('#itemcontainer');
        // the files are served by the node the browser is connected to, so
        // there is nothing to show for the other nodes of a cluster
        if (!nodename || !container || nodename !== Proxmox.NodeName) {
            return;
        }

        var provider = Ext.state.Manager.getProvider();
        var meta = null;
        var charts = [];
        var signature = '';

        var store = Ext.create('Proxmox.data.UpdateStore', {
            interval: 30000,
            fields: [{ type: 'date', dateFormat: 'timestamp', name: 'time' }],
            proxy: {
                type: 'ajax',
                url: dataUrl(provider.get(STATE_ID)),
                pageParam: '',
                startParam: '',
                limitParam: '',
                reader: {
                    type: 'json',
                    rootProperty: 'data',
                    transform: function (raw) {
                        meta = raw;
                        return raw;
                    },
                },
            },
        });

        var rebuild = function () {
            if (summary.destroyed || summary.destroying || container.destroyed) {
                return;
            }
            var gpus = meta && meta.node === nodename && Ext.isArray(meta.gpus) ? meta.gpus : [];
            var newSignature = Ext.encode(gpus.map(function (gpu) {
                return [gpu.id, gpu.label, gpu.mem_total];
            }));
            if (newSignature === signature) {
                return;
            }
            signature = newSignature;

            Ext.suspendLayouts();
            try {
                charts.forEach(function (chart) {
                    try {
                        container.remove(chart, true);
                    } catch (err) {
                        warn(err);
                    }
                });
                charts = [];
                if (gpus.length) {
                    var index = insertIndex(container);
                    var columnWidth = 1 / (container.oldFactor || 1);
                    CHARTS.forEach(function (def) {
                        var chart = container.insert(index++, chartConfig(def, gpus, store, columnWidth));
                        // These charts are rebuilt when the list of GPUs changes and ExtJS
                        // fails to destroy a chart that is animating, so they never animate.
                        chart.setAnimation(false);
                        chart.setAnimation = Ext.emptyFn;
                        if (def.axisTitle && chart.axes && chart.axes[0]) {
                            chart.axes[0].setTitle(def.axisTitle);
                        }
                        charts.push(chart);
                    });
                }
            } finally {
                Ext.resumeLayouts(true);
            }
        };

        var safeRebuild = function () {
            try {
                rebuild();
            } catch (err) {
                warn(err);
            }
        };
        var rebuildLater = new Ext.util.DelayedTask(safeRebuild);
        var firstLoad = true;

        store.on('load', function (loadedStore, records, success) {
            if (!success) {
                return;
            }
            if (firstLoad) {
                firstLoad = false;
                safeRebuild();
            } else {
                // The list of GPUs rarely changes afterwards. When it does, wait until
                // the built-in charts have finished animating their own new data:
                // changing the layout under a running animation garbles them.
                rebuildLater.delay(1000);
            }
        });

        summary.mon(provider, 'statechange', function (prov, key, state) {
            if (key !== STATE_ID || !state) {
                return;
            }
            var url = dataUrl(state);
            if (store.getProxy().getUrl() !== url) {
                store.getProxy().setUrl(url);
                store.load();
            }
        });

        summary.on('activate', function () {
            store.startUpdate();
        });
        summary.on('destroy', function () {
            rebuildLater.cancel();
            store.stopUpdate();
            store.destroy();
        });
    }

    Ext.define('PVE.gpu.NodeSummary', {
        override: 'PVE.node.Summary',

        initComponent: function () {
            var me = this;
            me.callParent(arguments);
            try {
                attach(me);
            } catch (err) {
                // never break the Summary because of the GPU charts
                warn(err);
            }
        },
    });
})();
