import { useId, useState } from 'react'
import type { ShoeCatalogItem } from './types'

type Props = {
  catalog: ShoeCatalogItem[]
  onChoose: (item: ShoeCatalogItem) => void
  labels: { brand: string; model: string; color: string; choose: string; hint: string; preview: string }
}

export function ShoeCatalogPicker({ catalog, onChoose, labels }: Props) {
  const id = useId()
  const [brand, setBrand] = useState('')
  const [model, setModel] = useState('')
  const [color, setColor] = useState('')
  const [failedImage, setFailedImage] = useState<string | null>(null)
  const matches = catalog.filter(item => item.brand.toLowerCase() === brand.trim().toLowerCase())
  const variants = matches.filter(item => item.name.toLowerCase() === model.trim().toLowerCase())
  const selected = variants.find(item => item.colorway?.toLowerCase() === color.trim().toLowerCase()) ?? variants[0]
  const image = selected?.image_url
  return <section className="shoe-rules-form">
    <p>{labels.hint}</p>
    <div className="form-grid form-grid-two">
      <label>{labels.brand}<input list={`${id}-brands`} value={brand} onChange={event => { setBrand(event.target.value); setModel(''); setColor('') }} /></label>
      <datalist id={`${id}-brands`}>{[...new Set(catalog.map(item => item.brand))].sort().map(value => <option key={value} value={value} />)}</datalist>
      <label>{labels.model}<input list={`${id}-models`} value={model} onChange={event => { setModel(event.target.value); setColor('') }} /></label>
      <datalist id={`${id}-models`}>{[...new Set(matches.map(item => item.name))].sort().map(value => <option key={value} value={value} />)}</datalist>
      <label>{labels.color}<input list={`${id}-colors`} value={color} onChange={event => setColor(event.target.value)} /></label>
      <datalist id={`${id}-colors`}>{[...new Set(variants.map(item => item.colorway).filter(Boolean))].map(value => <option key={value} value={value!} />)}</datalist>
    </div>
    {image && failedImage !== image && <figure><img src={image} alt={`${selected.brand} ${selected.name}`} width="160" onError={() => setFailedImage(image)} /><figcaption>{labels.preview}</figcaption></figure>}
    <button type="button" className="button button-secondary" disabled={!brand.trim() || !model.trim()} onClick={() => onChoose({
      id: selected?.id ?? 'custom',
      brand: selected?.brand ?? brand.trim(),
      name: `${selected?.name ?? model.trim()}${color.trim() ? ` (${color.trim()})` : ''}`,
      image_url: image ?? null,
    })}>{labels.choose}</button>
  </section>
}
