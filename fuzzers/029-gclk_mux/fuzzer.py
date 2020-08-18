import operator
import itertools
from functools import reduce
from collections import defaultdict
from bitarray import bitarray

from util import database, toolchain, bitdiff, progress


with database.transact() as db:
    for device_name, device in db.items():
        progress(device_name)

        package, pinout = next(iter(device['pins'].items()))
        config_range = range(*device['ranges']['config'])
        config = device['config']

        def run(clocks, **kwargs):
            code = []
            outs = []
            for index, (pad_n, negedge) in enumerate(clocks):
                outs.append(f"Q{index}")
                if negedge:
                    code.append(f"wire N{index}; INV in{index}(C{pad_n}, N{index}); "
                                f"DFFE ff{index}(N{index}, E1, 1'b0, Q{index});")
                else:
                    code.append(f"DFFE ff{index}(C{pad_n}, E1, 1'b0, Q{index});")

            return toolchain.run(
                f"module top(input C1, C2, C3, E1, output QC, {', '.join(outs)}); "
                f"{' '.join(code)} "
                f"OR3 o(C1, C2, C3, QC); "
                f"endmodule",
                {
                    'C1': pinout[device['clocks']['1']['pad']],
                    'C2': pinout[device['clocks']['2']['pad']],
                    'C3': pinout[device['clocks']['3']['pad']],
                    'E1': pinout[device['enables']['1']['pad']],
                    **{
                        out: pinout[macrocell['pad']]
                        for out, macrocell in zip(outs, device['macrocells'].values())
                    },
                    'QC': pinout[device['macrocells']['MC8']['pad']],
                },
                f"{device_name}-{package}", **kwargs)

        # Trace the global clock routes from the pads to the macrocell FF clock inputs.
        def analyze(clocks, **kwargs):
            fuses = run(clocks, **kwargs)

            mapping = {}
            known_fuses = []
            for index, (pad_n, negedge) in enumerate(clocks):
                macrocell = list(device['macrocells'].values())[index]
                global_clock_option = macrocell['global_clock']
                global_clock_value = 0
                for n_fuse, fuse in enumerate(global_clock_option['fuses']):
                    global_clock_value += fuses[fuse] << n_fuse
                    known_fuses.append(fuse)
                for global_clock_net, global_clock_net_value in \
                        global_clock_option['values'].items():
                    if global_clock_value == global_clock_net_value:
                        break
                else:
                    assert False
                mapping[f"{global_clock_net}_mux"] = f"c{pad_n}"

                gclk_invert_option = device['config'][f"{global_clock_net}_invert"]
                for n_fuse, fuse in enumerate(gclk_invert_option['fuses']):
                    assert fuses[fuse] == negedge
                    known_fuses.append(fuse)
                    break
                else:
                    assert False

            for fuse in known_fuses:
                # We know the exact meaning of these fuses, so exclude them from further analysis.
                fuses[fuse] = 0

            return fuses, {k: v for k, v in sorted(mapping.items())}

        results = defaultdict(lambda: [])
        for cx_pad_n, cx_invert, cy_pad_n, cy_invert, cz_invert, ct_pad_n in \
                itertools.product('123', [0,1], '123', [0,1], [0,1], '123'):
            if cx_pad_n == cy_pad_n: continue
            if cy_invert == cz_invert: continue
            try:
                fuses, mapping = analyze([(cx_pad_n, cx_invert),
                                          (cy_pad_n, cy_invert),
                                          (cy_pad_n, cz_invert)],
                                         strategy={'twoclock': f"C{ct_pad_n}"})
                if len(mapping) < 3: continue
                fuses_noconfig = bitarray(fuses)
                fuses_noconfig[config_range.start:config_range.stop] = \
                    bitarray([0]*len(config_range))
                results[fuses_noconfig.tobytes()].append((mapping, fuses))
            except toolchain.FitterError:
                pass

        # Get rid of all configurations that have different netlists, modulo known fuses.
        # The fitter doesn't strongly normalize so we get two different netlists here, ignoring
        # the differences in the GCLK mux.
        results = max(results.values(), key=lambda result: len(result))

        config.update(bitdiff.correlate({
            'gclk1_mux': 2,
            'gclk2_mux': 2,
            'gclk3_mux': 2,
        }, results))
