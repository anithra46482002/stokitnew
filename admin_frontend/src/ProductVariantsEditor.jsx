import React from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { blankVariant, gstPercentageError, updateVariantField } from './variantGst';

// Inline, optional Product Variants section of the Add Product form. It only edits the rows it is given; the
// parent form saves them (after the product) with the existing product-variants API.
export default function ProductVariantsEditor({ variants, onChange, productGstPercentage = '0', disabled = false }) {
  const edit = (uid, key, value) => onChange(variants.map((row) => (row.uid === uid ? updateVariantField(row, key, value) : row)));
  const remove = (uid) => onChange(variants.filter((row) => row.uid !== uid));
  return (
    <section className="product-variants-editor" aria-labelledby="product-variants-title">
      <div className="page-heading">
        <h3 id="product-variants-title">Product Variants</h3>
      </div>
      <small className="field-help">Optional. Rows without a variant name are not saved. GST Price is the price multiplied by the GST percentage, divided by 100.</small>
      {variants.map((row, index) => {
        const label = 'Variant ' + (index + 1);
        const gstOn = String(row.gst || 'N').toUpperCase() === 'Y';
        const percentageError = gstPercentageError(row.gst_percentage);
        return (
          <div className="product-variant-row" key={row.uid} role="group" aria-label={label}>
            <label><span>Variant Name</span><input type="text" aria-label={label + ' name'} value={row.name} onChange={(event) => edit(row.uid, 'name', event.target.value)} disabled={disabled} /></label>
            <label><span>Price</span><input type="number" min="0" step="any" aria-label={label + ' price'} value={row.price} onChange={(event) => edit(row.uid, 'price', event.target.value)} disabled={disabled} /></label>
            <label><span>Discount (%)</span><input type="number" min="0" max="100" step="any" aria-label={label + ' discount'} value={row.discount} placeholder="0" onChange={(event) => edit(row.uid, 'discount', event.target.value)} disabled={disabled} /></label>
            <label><span>Quantity</span><input type="number" min="0" step="1" aria-label={label + ' quantity'} value={row.quantity} onChange={(event) => edit(row.uid, 'quantity', event.target.value)} disabled={disabled} /></label>
            <label><span>GST Applicable</span><select aria-label={label + ' GST applicable'} value={gstOn ? 'Y' : 'N'} onChange={(event) => edit(row.uid, 'gst', event.target.value)} disabled={disabled}><option value="N">No</option><option value="Y">Yes</option></select></label>
            <label>
              <span>GST Percentage (%)</span>
              <input type="number" min="0" max="100" step="any" aria-label={label + ' GST percentage'} aria-invalid={percentageError ? true : undefined} value={row.gst_percentage} onChange={(event) => edit(row.uid, 'gst_percentage', event.target.value)} disabled={disabled || !gstOn} />
              {percentageError && <small className="error" role="alert">{percentageError}</small>}
            </label>
            <label><span>GST Price</span><input type="text" readOnly aria-label={label + ' GST price'} value={row.gst_price} /></label>
            <button type="button" className="sk-btn-delete" aria-label={'Remove ' + label} onClick={() => remove(row.uid)} disabled={disabled}><Trash2 size={15} /> Remove</button>
          </div>
        );
      })}
      <div className="product-variants-add-bottom">
        <button type="button" className="sk-btn-edit" onClick={() => onChange([...variants, blankVariant(productGstPercentage)])} disabled={disabled}><Plus size={15} /> Add Variant</button>
      </div>
    </section>
  );
}