// Elements
const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');
const uploadContent = document.getElementById('uploadContent');
const imagePreview = document.getElementById('imagePreview');
const generateBtn = document.getElementById('generateBtn');
const optionBtns = document.querySelectorAll('.option-btn');
const colorCircles = document.querySelectorAll('.color-circle');
const resultArea = document.getElementById('resultArea');
const resultImage = document.getElementById('resultImage');
const resetBtn = document.getElementById('resetBtn');
const downloadBtn = document.getElementById('downloadBtn');
const btnText = generateBtn.querySelector('.btn-text');
const loadingSpinner = document.getElementById('loadingSpinner');

let currentFile = null;
let currentStyle = 'id-photo';
let currentBgColor = 'blue';

// --- Event Listeners for UI ---

// Drag & Drop
uploadZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadZone.classList.add('dragover');
});

uploadZone.addEventListener('dragleave', () => {
  uploadZone.classList.remove('dragover');
});

uploadZone.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadZone.classList.remove('dragover');
  if (e.dataTransfer.files.length > 0) {
    handleFile(e.dataTransfer.files[0]);
  }
});

// File Input
fileInput.addEventListener('change', (e) => {
  if (e.target.files.length > 0) {
    handleFile(e.target.files[0]);
  }
});

// Settings Selection
optionBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    optionBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentStyle = btn.dataset.style;
  });
});

colorCircles.forEach(circle => {
  circle.addEventListener('click', () => {
    colorCircles.forEach(c => c.classList.remove('active'));
    circle.classList.add('active');
    currentBgColor = circle.dataset.color;
  });
});

// Reset
resetBtn.addEventListener('click', () => {
  currentFile = null;
  fileInput.value = '';
  imagePreview.src = '';
  imagePreview.classList.add('hidden');
  uploadContent.classList.remove('hidden');
  generateBtn.disabled = true;
  resultArea.classList.add('hidden');
});

// Handle Uploaded File
function handleFile(file) {
  if (!file.type.startsWith('image/')) {
    alert('Please upload an image file.');
    return;
  }
  
  currentFile = file;
  const reader = new FileReader();
  reader.onload = (e) => {
    imagePreview.src = e.target.result;
    imagePreview.classList.remove('hidden');
    uploadContent.classList.add('hidden');
    generateBtn.disabled = false;
  };
  reader.readAsDataURL(file);
}

// Generate Action
generateBtn.addEventListener('click', async () => {
  if (!currentFile) return;

  // UI state: loading
  generateBtn.disabled = true;
  btnText.textContent = 'Processing with AI...';
  loadingSpinner.classList.remove('hidden');
  
  resultArea.classList.remove('hidden');
  resultImage.src = imagePreview.src; 
  resultImage.parentElement.classList.add('processing');

  try {
    // Attempt to call OpenAI API
    // Note: In production, API calls should be routed through a backend (like Cloudflare Worker).
    // For this open-source demo, we use Vite env variables.
    const apiKey = import.meta.env.VITE_OPENAI_API_KEY;
    
    if (!apiKey) {
      // Simulation mode if no key provided
      await new Promise(r => setTimeout(r, 2500));
      finishGeneration('https://images.unsplash.com/photo-1544005313-94ddf0286df2?auto=format&fit=crop&q=80&w=400&h=533'); // Mock result
      alert('Mock Mode: No API key found. Showing sample result.');
      return;
    }

    // Example OpenAI API Call (DALL-E or Image Edit Endpoint)
    // We mock the fetch structure for demonstration of AI connectivity:
    /*
    const formData = new FormData();
    formData.append('image', currentFile);
    formData.append('prompt', `Generate a professional ${currentStyle} portrait with ${currentBgColor} background.`);
    
    const response = await fetch('https://api.openai.com/v1/images/edits', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${apiKey}` },
      body: formData
    });
    const data = await response.json();
    finishGeneration(data.data[0].url);
    */
    
    // Simulate API delay
    await new Promise(r => setTimeout(r, 2000));
    finishGeneration(imagePreview.src);

  } catch (error) {
    console.error('AI Generation Failed:', error);
    alert('Failed to generate portrait. Check console for details.');
  } finally {
    generateBtn.disabled = false;
    btnText.textContent = 'Generate with AI';
    loadingSpinner.classList.add('hidden');
    resultImage.parentElement.classList.remove('processing');
  }
});

function finishGeneration(url) {
  resultImage.src = url;
  
  // Download handler
  downloadBtn.onclick = () => {
    const a = document.createElement('a');
    a.href = url;
    a.download = `Auralis_Portrait_${currentStyle}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };
}
