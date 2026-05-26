import { itemColor } from "./entityVisuals";

export function Block({
  position,
  rotation,
  size,
  color,
  opacity = 1,
  wireframe = false,
}: {
  position: [number, number, number];
  rotation?: [number, number, number];
  size: [number, number, number];
  color: string;
  opacity?: number;
  wireframe?: boolean;
}) {
  return (
    <mesh position={position} rotation={rotation} scale={size} castShadow receiveShadow>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color={color} transparent={opacity < 1} opacity={opacity} wireframe={wireframe} roughness={0.78} metalness={0.05} />
    </mesh>
  );
}

export function CylinderBlock({
  position,
  rotation,
  radius = 0.28,
  depth = 0.5,
  color,
}: {
  position: [number, number, number];
  rotation?: [number, number, number];
  radius?: number;
  depth?: number;
  color: string;
}) {
  return (
    <mesh position={position} rotation={rotation} castShadow receiveShadow>
      <cylinderGeometry args={[radius, radius, depth, 14]} />
      <meshStandardMaterial color={color} roughness={0.74} metalness={0.04} />
    </mesh>
  );
}

function normalizedItemType(itemType: unknown): string {
  return String(itemType ?? "").trim().toLowerCase();
}

export function ItemShape({
  itemType,
  position,
  scale = 1,
}: {
  itemType: unknown;
  position: [number, number, number];
  scale?: number;
}) {
  const normalized = normalizedItemType(itemType);
  const color = itemColor(normalized);

  if (normalized.includes("battery")) {
    return (
      <group position={position} scale={[scale, scale, scale]}>
        <Block position={[0, 0, 0]} size={[0.34, 0.62, 0.28]} color={color} />
        <Block position={[0, 0.36, 0]} size={[0.2, 0.1, 0.18]} color="#263b54" />
        <Block position={[0, 0.02, 0.16]} size={[0.2, 0.34, 0.03]} color="#fff3b0" />
      </group>
    );
  }

  if (normalized.includes("product")) {
    return (
      <group position={position} scale={[scale, scale, scale]}>
        <Block position={[0, 0, 0]} size={[0.56, 0.44, 0.56]} color={color} />
        <Block position={[0, 0.24, 0]} size={[0.38, 0.06, 0.38]} color="#eafff1" />
      </group>
    );
  }

  if (normalized.includes("intermediate") || normalized.includes("transfer")) {
    return (
      <group position={position} scale={[scale, scale, scale]}>
        <CylinderBlock position={[0, 0, 0]} rotation={[Math.PI / 2, 0, 0]} radius={0.23} depth={0.52} color={color} />
        <Block position={[0, 0, 0]} size={[0.5, 0.18, 0.18]} color="#d9f8ff" opacity={0.82} />
      </group>
    );
  }

  if (normalized.includes("scrap") || normalized.includes("waste")) {
    return (
      <group position={position} scale={[scale, scale, scale]}>
        <Block position={[-0.08, 0, 0.02]} rotation={[0.18, 0.2, 0.1]} size={[0.34, 0.22, 0.28]} color="#ff6b7a" />
        <Block position={[0.12, 0.08, -0.08]} rotation={[-0.12, 0.35, -0.18]} size={[0.28, 0.18, 0.22]} color="#8d1f33" />
      </group>
    );
  }

  return (
    <group position={position} scale={[scale, scale, scale]}>
      <Block position={[0, 0, 0]} size={[0.46, 0.38, 0.46]} color={color} />
      <Block position={[0, 0.22, 0]} size={[0.36, 0.06, 0.36]} color="#dceaff" />
    </group>
  );
}

export function HumanoidBlockModel({
  color,
  cargoId,
  cargoType,
  walkSwing = 0,
  workSwing = 0,
}: {
  color: string;
  cargoId?: string;
  cargoType?: unknown;
  walkSwing?: number;
  workSwing?: number;
}) {
  const hasCargo = Boolean(cargoId || cargoType);
  const leftArmSwing = hasCargo ? -0.55 + workSwing * 0.18 : walkSwing * 0.7 + workSwing;
  const rightArmSwing = hasCargo ? -0.55 - workSwing * 0.18 : -walkSwing * 0.7 - workSwing;

  return (
    <group>
      <Block position={[-0.18, 0.35, 0]} rotation={[walkSwing, 0, 0]} size={[0.25, 0.7, 0.25]} color="#263b54" />
      <Block position={[0.18, 0.35, 0]} rotation={[-walkSwing, 0, 0]} size={[0.25, 0.7, 0.25]} color="#263b54" />
      <Block position={[0, 1.05, 0]} size={[0.72, 0.85, 0.46]} color={color} />
      <Block position={[0, 1.73, 0]} size={[0.58, 0.48, 0.52]} color="#eff7ff" />
      <Block position={[0, 1.76, 0.28]} size={[0.4, 0.12, 0.04]} color="#29a8ff" />
      <Block position={[-0.5, 1.08, 0.04]} rotation={[leftArmSwing, 0, 0]} size={[0.18, 0.72, 0.18]} color="#8ea6c0" />
      <Block position={[0.5, 1.08, 0.04]} rotation={[rightArmSwing, 0, 0]} size={[0.18, 0.72, 0.18]} color="#8ea6c0" />
      {hasCargo && (
        <group position={[0, 0, 0]}>
          <Block position={[0, 0.86, 0.57]} size={[0.72, 0.08, 0.12]} color="#263b54" />
          <ItemShape itemType={cargoType || cargoId} position={[0, 1.04, 0.76]} scale={0.9} />
        </group>
      )}
    </group>
  );
}
