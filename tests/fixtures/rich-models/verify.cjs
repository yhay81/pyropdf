const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const loadOpenCascade = require("occt-import-js");
const gltfValidator = require("gltf-validator");

const directory = process.argv[2] || ".";
const read = (name) => Uint8Array.from(fs.readFileSync(path.join(directory, name)));

async function verifyStep() {
  const occt = await loadOpenCascade();
  const bytes = read("triangle.step");
  const result = occt.ReadStepFile(bytes, null);
  assert.equal(result.success, true);
  assert.equal(result.meshes.length, 1);
  const mesh = result.meshes[0];
  const points = mesh.attributes.position.array;
  assert.equal(points.length, 9);
  const vertices = [0, 3, 6].map((start) => points.slice(start, start + 3).join(","));
  assert.deepEqual(vertices.sort(), ["0,0,0", "0,4,0", "3,0,0"]);
  assert.equal(mesh.brep_faces.length, 1);
  assert.deepEqual([...mesh.index.array].sort(), [0, 1, 2]);
  const entities = new TextDecoder().decode(bytes).match(/^#\d+=/gm).length;
  assert.equal(entities, 44);
  console.log("STEP: 44 entities, 1 mesh, 1 B-rep face, 3 vertices, 1 triangle");
}

async function verifyGltf(name, expectedResources) {
  const requested = [];
  const report = await gltfValidator.validateBytes(read(name), {
    uri: name,
    externalResourceFunction: async (resource) => {
      assert.equal(resource, "triangle.bin");
      requested.push(resource);
      return read(resource);
    }
  });
  const { numErrors, numWarnings, numInfos, numHints } = report.issues;
  assert.deepEqual([numErrors, numWarnings, numInfos, numHints], [0, 0, 0, 0]);
  const { totalVertexCount, totalTriangleCount, drawCallCount } = report.info;
  assert.deepEqual([totalVertexCount, totalTriangleCount, drawCallCount], [3, 1, 1]);
  assert.deepEqual(requested, expectedResources);
  console.log(name, "3 vertices, 1 triangle, 1 draw, clean;", "external resources", requested);
}

async function main() {
  await verifyStep();
  await verifyGltf("triangle.gltf", ["triangle.bin"]);
  await verifyGltf("triangle-inline.gltf", []);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
