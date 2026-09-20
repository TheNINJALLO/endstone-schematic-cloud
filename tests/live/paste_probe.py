import json
import os
import traceback
import hashlib
from pathlib import Path

from endstone_ninjos_schematics.plugin import NinjOSSchematicsPlugin, PLUGIN_VERSION
from endstone_ninjos_schematics.models import BlockPos, PasteJob, PastePlan, PasteChunkRange
from endstone_ninjos_schematics.codec import append_record


class Probe(NinjOSSchematicsPlugin):
    version = '1.0.0'
    commands = {}
    permissions = {}
    soft_depend = []

    def on_enable(self):
        self._skip_unchanged = True
        self._apply_physics = False
        self._verify_paste_writes = True
        self._max_paste_failures = 0
        self._missing_block_policy = 'abort'
        self._history_max_blocks_per_operation = 100
        self.results = []
        self.task = self.server.scheduler.run_task(self, self.probe, delay=100, period=20)
        self.waits = 0
        self._tick_counter = 0

    def on_disable(self):
        pass

    def probe(self):
        try:
            dim = self.server.level.get_dimension('Overworld')
            if self.waits == 0:
                self.server.dispatch_command(self.server.command_sender, 'tickingarea add circle 0 90 0 1 schem_probe true')
            self.waits += 1
            if self.waits < 3 or not any(c.x == 0 and c.z == 0 for c in dim.loaded_chunks):
                if self.waits > 60:
                    raise RuntimeError('Chunk failed to load')
                return
            cases = [
                ('minecraft:stone', {}),
                ('minecraft:oak_stairs', {}),
                ('minecraft:oak_stairs', {'weirdo_direction': 2}),
                ('minecraft:chest', {}),
                ('minecraft:grass', {}),
                ('minecraft:wooden_door', {}),
                ('minecraft:water', {}),
                ('minecraft:torch', {}),
                ('minecraft:oak_log', {'pillar_axis': 'x'}),
                ('minecraft:stone', {'stone_type': 'granite'}),
            ]
            for index, (kind, states) in enumerate(cases):
                pos = BlockPos(index, 90, 0)
                dim.get_block_at(pos.x, pos.y - 1, pos.z).set_type('minecraft:stone', apply_physics=False)
                block = dim.get_block_at(pos.x, pos.y, pos.z)
                block.set_type('minecraft:air', apply_physics=False)
                record = bytearray()
                append_record(record, 0, 0, 0, 0)
                plan = PastePlan((1, 1, 1), [{'type': kind, 'states': states}], bytes(record), (PasteChunkRange(0, 0, 0, 1),))
                job = PasteJob('probe', kind, plan, 'Overworld', pos, 0, capture_history=True)
                result = {'requested_type': kind, 'requested_states': states}
                try:
                    resolved = self.server.create_block_data(kind, states)
                    result['resolved_type'] = self.block_data_identifier(resolved)
                    result['resolved_states'] = dict(resolved.block_states)
                    self._paste_batch(job, dim, 1)
                    result['passed'] = job.placed == 1 and job.failed == 0
                except Exception as error:
                    result.update(passed=False, error=str(error))
                result['actual_type'] = self.block_data_identifier(block.data)
                result['actual_states'] = dict(block.data.block_states)
                result['captured_blocks'] = job.captured_blocks
                if result.get('passed'):
                    expected = (result['resolved_type'], result['resolved_states'])
                    unchanged = PasteJob('probe', kind, plan, 'Overworld', pos, 0)
                    self._paste_batch(unchanged, dim, 1)
                    result['unchanged_skipped'] = unchanged.skipped == 1 and unchanged.write_attempts == 0
                    history = self._history_entry_from_job(job)
                    undo = PasteJob('probe', kind, history.before_plan, 'Overworld', pos, 0, operation='undo')
                    self._paste_batch(undo, dim, 1)
                    result['undo_passed'] = self.block_data_identifier(block.data) == 'minecraft:air'
                    redo = PasteJob('probe', kind, history.after_plan, 'Overworld', pos, 0, operation='redo')
                    self._paste_batch(redo, dim, 1)
                    result['redo_passed'] = (self.block_data_identifier(block.data), dict(block.data.block_states)) == expected
                self.results.append(result)
            self.finish()
        except Exception:
            self.results.append({'fatal': traceback.format_exc()})
            self.finish()

    def finish(self):
        self.task.cancel()
        Path(os.environ['SCHEM_PROBE_OUTPUT']).write_text(json.dumps({'version': PLUGIN_VERSION, 'plugin_source_sha256': hashlib.sha256(Path(__import__('endstone_ninjos_schematics.plugin', fromlist=['__file__']).__file__).read_bytes()).hexdigest(), 'checks': self.results}, indent=2))
        self.logger.info('SCHEM_PROBE_COMPLETE')
