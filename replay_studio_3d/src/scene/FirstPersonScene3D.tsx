import { Canvas, useThree } from "@react-three/fiber";
import { PerspectiveCamera } from "@react-three/drei";
import { useEffect, useMemo } from "react";
import type { ReplayEvent } from "../replay-core/types/event";
import type { BaseEntityState } from "../replay-core/types/entity";
import type { ReplayRenderModel } from "../replay-core/types/replay";
import { Block, ItemShape } from "./blockModels";
import { cargoItemId, cargoItemType } from "./entityVisuals";
import { createCoordinateMapper, workerCameraPose, type WorkerCameraPose } from "./coordinates";
import { FactoryWorldContents, type SelectHandler } from "./FactoryScene3D";

interface FirstPersonScene3DProps {
  renderModel: ReplayRenderModel;
  currentEvent?: ReplayEvent;
  currentTime: number;
  viewport: { width: number; height: number };
  worker?: BaseEntityState;
  onSelectEntity?: SelectHandler;
}

function FirstPersonCamera({ pose }: { pose: WorkerCameraPose }) {
  const { camera } = useThree();

  useEffect(() => {
    camera.position.set(pose.position.x, pose.position.y, pose.position.z);
    camera.lookAt(pose.target.x, pose.target.y, pose.target.z);
    camera.updateProjectionMatrix();
  }, [camera, pose]);

  return null;
}

function FirstPersonForeground({ pose, worker }: { pose: WorkerCameraPose; worker: BaseEntityState }) {
  const cargoId = cargoItemId(worker);
  const cargoType = cargoItemType(worker) || cargoId;
  const hasCargo = Boolean(cargoId || cargoType);
  const rotationY = Math.atan2(Math.cos(pose.headingAngle), Math.sin(pose.headingAngle));

  return (
    <group position={[pose.position.x, pose.position.y - 0.56, pose.position.z]} rotation={[0, rotationY, 0]}>
      <Block position={[-0.23, -0.02, 0.78]} rotation={[0.12, 0, -0.08]} size={[0.12, 0.16, 0.42]} color="#8ea6c0" />
      <Block position={[0.23, -0.02, 0.78]} rotation={[0.12, 0, 0.08]} size={[0.12, 0.16, 0.42]} color="#8ea6c0" />
      <Block position={[-0.12, -0.1, 1.0]} size={[0.16, 0.08, 0.12]} color="#263b54" />
      <Block position={[0.12, -0.1, 1.0]} size={[0.16, 0.08, 0.12]} color="#263b54" />
      {hasCargo && <ItemShape itemType={cargoType || cargoId} position={[0, -0.08, 1.08]} scale={0.78} />}
    </group>
  );
}

export function FirstPersonScene3D({ renderModel, currentEvent, currentTime, viewport, worker, onSelectEntity }: FirstPersonScene3DProps) {
  const mapper = useMemo(() => createCoordinateMapper(renderModel.grid, viewport), [renderModel.grid, viewport]);
  const pose = useMemo(() => workerCameraPose(worker, currentTime, mapper), [currentTime, mapper, worker]);
  const hiddenEntityIds = useMemo(() => (worker ? new Set([worker.entity_id]) : new Set<string>()), [worker]);

  if (!worker || !pose) {
    return <div className="first-person-empty">No worker selected</div>;
  }

  return (
    <Canvas shadows gl={{ antialias: true, preserveDrawingBuffer: true }}>
      <color attach="background" args={["#dfeaf5"]} />
      <ambientLight intensity={0.68} />
      <directionalLight position={[18, 36, 16]} intensity={1.08} castShadow />
      <PerspectiveCamera makeDefault fov={66} near={0.03} far={180} />
      <FirstPersonCamera pose={pose} />
      <FactoryWorldContents
        renderModel={renderModel}
        mapper={mapper}
        currentTime={currentTime}
        currentEvent={currentEvent}
        hiddenEntityIds={hiddenEntityIds}
        showMotionPaths={false}
        onSelectEntity={onSelectEntity}
      />
      <FirstPersonForeground pose={pose} worker={worker} />
    </Canvas>
  );
}
