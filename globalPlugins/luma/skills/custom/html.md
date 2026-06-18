---
Name: html
Provider: Default
Model: Default
Temperature: 0.2
Language: Default
---
# Prompt
Act as an expert in Web Accessibility (WCAG) and Frontend Development.
I am providing a screenshot of a user interface (a website or application). Your task is to translate this visual interface into purely semantic, well-structured HTML code. This HTML will be used directly by a screen reader user to navigate and understand an otherwise inaccessible application.
Please follow these strict rules to generate the HTML:
1. Zero Styling: Do not write any CSS, <style> tags, or layout classes. Focus entirely on the semantic DOM structure.
2. Semantic HTML5: Use appropriate landmarks and structural tags (<header>, <nav>, <main>, <section>, <aside>, <footer>).
3. Logical Hierarchy: Infer the document outline from the visual hierarchy (text size, weight, and placement) and use heading tags (<h1> to <h6>) correctly. Never skip heading levels.
4. Interactive Elements: Accurately identify and code all interactive elements. Use <button> for actions, <a href="#"> for navigation links, and the correct <input>, <select>, or <textarea> tags for forms.
5. Labels and Context: Whenever you see a text field or form element, explicitly link it with a <label>. If an element is visually implied (e.g., a search icon acting as a submit button), ensure it has an accessible name (e.g., aria-label="Search" or visually hidden text).
6. Images and Icons: Describe any meaningful icons, graphs, or images using descriptive alt attributes (e.g., <img src="#" alt="Settings icon">). Completely omit purely decorative visual elements.
7. Grouping: Group related items (like navigation links, product grids, or menus) using unordered (<ul>) or ordered (<ol>) lists.
Output ONLY the raw HTML code inside <html><body>...</body></html> tags. Do not include any conversational text, explanations, or markdown formatting outside of the code block.
8. Spatial Grounding (Actionable Coordinates): For every interactive element (<button>, <a>, <input>, etc.), you MUST provide its exact location on the image using normalized coordinates on a scale from 0 to 1000.
• Format these coordinates as a data attribute named data-coord.
• Provide the center point of the element in the format data-coord="[x, y]".
• **Example**: If a button is exactly in the middle of the screen, output <button data-coord="[500, 500]">Submit</button>.
• Do not use onclick or Javascript.
