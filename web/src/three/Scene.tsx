import { Canvas } from '@react-three/fiber'
import { SoftShadows } from '@react-three/drei'
import { useStore } from '../store'
import { CameraRig } from './CameraRig'
import { House } from './House'
import { Projector } from './anchors'

export function Scene() {
  const selectPiece = useStore(s => s.selectPiece)
  return (
    <Canvas
      className="scene"
      shadows
      dpr={[1, 2]}
      camera={{ fov: 38, position: [5, 13, 15], near: 0.05, far: 120 }}
      gl={{ antialias: true, alpha: true }}
      onPointerMissed={() => selectPiece(null)}
    >
      <SoftShadows size={22} samples={10} focus={0.5} />
      <hemisphereLight args={['#fff8ee', '#d8c8b0', 1.15]} />
      <directionalLight
        position={[7, 13, 9]}
        intensity={1.9}
        color="#fff3e2"
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-bias={-0.0004}
        shadow-camera-left={-10}
        shadow-camera-right={10}
        shadow-camera-top={10}
        shadow-camera-bottom={-10}
      />
      <directionalLight position={[-8, 6, -4]} intensity={0.35} color="#dfe8ff" />
      <House />
      <CameraRig />
      <Projector />
    </Canvas>
  )
}
